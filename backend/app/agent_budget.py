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
        connection.execute("INSERT INTO model_budget_accounts(budget_id,limit_cents) VALUES ('project',?) ON CONFLICT (budget_id) DO NOTHING", (self.limit,))
        account = connection.execute("SELECT * FROM model_budget_accounts WHERE budget_id='project'").fetchone()
        # Legacy unknown reservations are conservatively charged. Never erase
        # historical costs even when the originating session is later deleted.
        delta = max(0, legacy - account['imported_legacy_cents'])
        connection.execute("""UPDATE model_budget_accounts
            SET spent_cents=spent_cents+?,
                imported_legacy_cents=CASE WHEN imported_legacy_cents > ? THEN imported_legacy_cents ELSE ? END,
                limit_cents=CASE WHEN limit_cents < ? THEN limit_cents ELSE ? END
            WHERE budget_id='project'""", (delta, legacy, legacy, self.limit, self.limit))
        return connection.execute("SELECT * FROM model_budget_accounts WHERE budget_id='project'").fetchone()

    def status(self):
        with database(self.path) as connection, transaction(connection):
            account = self._account(connection)
            # PostgreSQL SUM(bigint) is Decimal; the public cents contract is int.
            reserved = int(connection.execute("SELECT coalesce(sum(reserved_cents),0) FROM model_budget_reservations WHERE status='reserved'").fetchone()[0])
            return {'limitCents': account['limit_cents'], 'spentCents': account['spent_cents'],
                    'reservedCents': reserved, 'remainingCents': max(0, account['limit_cents'] - account['spent_cents'] - reserved)}

    def reserve_request(self, reservation_id, cents):
        """Claim one named request, without joining the legacy model FIFO."""
        if not isinstance(reservation_id, str) or not 1 <= len(reservation_id) <= 256 or type(cents) is not int or not 1 <= cents <= 3000:
            raise RepositoryError('invalid_input', '请求预算标识或预留金额无效。')
        with database(self.path) as connection, transaction(connection):
            old = connection.execute('SELECT * FROM model_budget_reservations WHERE reservation_id=?', (reservation_id,)).fetchone()
            if old:
                if old['run_id'] != self.run_id or old['reserved_cents'] != cents:
                    raise RepositoryError('request_conflict', '费用编号已用于其他请求。')
                return False
            account = self._account(connection)
            reserved = connection.execute("SELECT coalesce(sum(reserved_cents),0) FROM model_budget_reservations WHERE status='reserved'").fetchone()[0]
            if account['spent_cents'] + reserved + cents > account['limit_cents']:
                return False
            connection.execute("INSERT INTO model_budget_reservations(reservation_id,run_id,reserved_cents,status) VALUES (?,?,?,'reserved')",
                               (reservation_id, self.run_id, cents))
            return True

    def settle_request(self, reservation_id, actual_cents=None):
        """Settle the specified request once. Unknown costs consume its upper bound."""
        if actual_cents is not None and (type(actual_cents) is not int or actual_cents < 0):
            raise RepositoryError('invalid_input', '请求结算金额无效。')
        with database(self.path) as connection, transaction(connection):
            row = connection.execute('SELECT * FROM model_budget_reservations WHERE reservation_id=?', (reservation_id,)).fetchone()
            if row is None or row['run_id'] != self.run_id:
                raise RepositoryError('scope_violation', '费用记录不属于当前请求。', False)
            if row['status'] != 'reserved':
                return {'reservationId': reservation_id, 'status': row['status'], 'chargedCents': row['actual_cents']}
            if actual_cents is not None and actual_cents > row['reserved_cents']:
                raise RepositoryError('invalid_input', '实际费用超过预留，需人工核对。')
            charge = row['reserved_cents'] if actual_cents is None else actual_cents
            self._account(connection)
            status = 'unknown' if actual_cents is None else 'settled'
            connection.execute('UPDATE model_budget_reservations SET actual_cents=?,status=? WHERE reservation_id=?', (charge, status, reservation_id))
            connection.execute("UPDATE model_budget_accounts SET spent_cents=spent_cents+? WHERE budget_id='project'", (charge,))
            return {'reservationId': reservation_id, 'status': status, 'chargedCents': charge}

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
