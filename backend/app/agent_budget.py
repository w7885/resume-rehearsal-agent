"""Project-wide durable reservations, independent of session/day deletion."""
import json
from uuid import uuid4

from app.db import database, transaction
from app.repository import RepositoryError


class ProjectBudget:
    def __init__(self, path, run_id, limit_cents=3000):
        if type(limit_cents) is not int or not 1 <= limit_cents <= 3000:
            raise RepositoryError('invalid_input', '项目预算必须在 1～3000 分之间。')
        self.path, self.run_id, self.limit = path, run_id, limit_cents
        self.pending = []

    def _account(self, connection):
        legacy = 0
        for row in connection.execute("SELECT doc_json FROM agent_state WHERE scope='budget'"):
            doc = json.loads(row[0])
            for field in ('spent_cents', 'reserved_cents'):
                amount = doc.get(field, 0)
                if type(amount) is not int or not 0 <= amount <= 2**63-1-legacy:
                    raise RepositoryError('storage_failed', '既有预算记录需核对，暂不开放模型调用。', False)
                legacy += amount
        connection.execute("INSERT OR IGNORE INTO model_budget_accounts(budget_id,limit_cents) VALUES ('project',?)", (self.limit,))
        account = connection.execute("SELECT * FROM model_budget_accounts WHERE budget_id='project'").fetchone()
        # Legacy unknown reservations are conservatively charged. Never erase
        # historical costs even when the originating session is later deleted.
        delta = max(0, legacy - account['imported_legacy_cents'])
        connection.execute("UPDATE model_budget_accounts SET spent_cents=spent_cents+?, imported_legacy_cents=max(imported_legacy_cents,?), limit_cents=min(limit_cents,?) WHERE budget_id='project'", (delta, legacy, self.limit))
        return connection.execute("SELECT * FROM model_budget_accounts WHERE budget_id='project'").fetchone()

    def status(self):
        with database(self.path) as connection, transaction(connection):
            account = self._account(connection)
            reserved = connection.execute("SELECT coalesce(sum(reserved_cents),0) FROM model_budget_reservations WHERE status='reserved'").fetchone()[0]
            return {'limitCents': account['limit_cents'], 'spentCents': account['spent_cents'],
                    'reservedCents': reserved, 'remainingCents': max(0, account['limit_cents'] - account['spent_cents'] - reserved)}

    async def reserve(self, cents):
        if type(cents) is not int or cents < 1:
            raise RepositoryError('invalid_input', '预算预留金额无效。')
        with database(self.path) as connection, transaction(connection):
            account = self._account(connection)
            reserved = connection.execute("SELECT coalesce(sum(reserved_cents),0) FROM model_budget_reservations WHERE status='reserved'").fetchone()[0]
            if account['spent_cents'] + reserved + cents > account['limit_cents']:
                return False
            rid = f'reservation-{uuid4().hex}'
            connection.execute("INSERT INTO model_budget_reservations(reservation_id,run_id,reserved_cents,status) VALUES (?,?,?,'reserved')", (rid, self.run_id, cents))
        self.pending.append(rid)
        return True

    async def settle(self, run_id, actual_cents, reserved_cents):
        if type(actual_cents) is not int or actual_cents < 0:
            raise RepositoryError('invalid_input', '预算结算金额无效。')
        self._settle(actual_cents)

    async def charge_unknown(self, run_id, reserved_cents):
        self._settle(None)

    def _settle(self, actual):
        if not self.pending:
            return
        rid = self.pending[0]
        with database(self.path) as connection, transaction(connection):
            self._account(connection)
            row = connection.execute('SELECT * FROM model_budget_reservations WHERE reservation_id=?', (rid,)).fetchone()
            if row and row['status'] == 'reserved':
                charge = row['reserved_cents'] if actual is None else actual
                connection.execute('UPDATE model_budget_reservations SET actual_cents=?,status=? WHERE reservation_id=?', (charge, 'unknown' if actual is None else 'settled', rid))
                connection.execute("UPDATE model_budget_accounts SET spent_cents=spent_cents+? WHERE budget_id='project'", (charge,))
        self.pending.pop(0)

    async def close_unknown(self):
        while self.pending:
            self._settle(None)

    def snapshot(self):
        value = self.status()
        return value['spentCents'], value['reservedCents']
