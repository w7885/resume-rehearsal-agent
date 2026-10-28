"""Trusted, immutable input identity; never constructed from model arguments."""
from dataclasses import dataclass
import hashlib
import json


class SnapshotMismatch(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, repr=False)
class ProposalSnapshot:
    session_id: str
    target_id: str
    block_id: str
    state_revision: int
    draft_revision: int
    resume_generation: int
    block_revision: int
    before: str
    source_versions: tuple[tuple[str, int, str], ...]
    run_id: str | None = None

    def __post_init__(self):
        if any(not isinstance(value, str) or not value for value in
               (self.session_id, self.target_id, self.block_id)):
            raise ValueError('Snapshot identity must be nonempty')
        if any(type(value) is not int or value < 1 for value in
               (self.state_revision, self.draft_revision, self.resume_generation, self.block_revision)):
            raise ValueError('Snapshot revisions must be positive integers')
        if not isinstance(self.before, str) or not isinstance(self.source_versions, tuple):
            raise ValueError('Snapshot text and source versions must be immutable')
        for entry in self.source_versions:
            if (not isinstance(entry, tuple) or len(entry) != 3
                    or not isinstance(entry[0], str) or not entry[0]
                    or type(entry[1]) is not int or entry[1] < 1
                    or entry[2] not in {'resume', 'supplement', 'jd', 'other'}):
                raise ValueError('Invalid immutable source version')
        if len({item[0] for item in self.source_versions}) != len(self.source_versions):
            raise ValueError('Duplicate snapshot source identity')
        if self.run_id is not None and (not isinstance(self.run_id, str) or not self.run_id):
            raise ValueError('Invalid snapshot run identity')

    @property
    def before_sha256(self):
        return hashlib.sha256(self.before.encode('utf-8')).hexdigest()

    @property
    def fingerprint(self):
        fields = {key: getattr(self, key) for key in
                  ('session_id', 'target_id', 'block_id', 'state_revision', 'draft_revision',
                   'resume_generation', 'block_revision', 'source_versions', 'run_id')}
        fields['before_sha256'] = self.before_sha256
        return hashlib.sha256(json.dumps(fields, sort_keys=True).encode('utf-8')).hexdigest()

    def verify(self, *, session_id, block_id, state_revision, draft_revision,
               resume_generation, block_revision, before, source_versions, run_id):
        if session_id != self.session_id or block_id != self.block_id:
            raise SnapshotMismatch('scope_violation', '目标不属于本次生成输入快照。')
        if run_id != self.run_id:
            raise SnapshotMismatch('stale_state', '生成运行与输入快照不一致。')
        actual = (state_revision, draft_revision, resume_generation, block_revision, before)
        expected = (self.state_revision, self.draft_revision, self.resume_generation,
                    self.block_revision, self.before)
        if actual != expected:
            raise SnapshotMismatch('stale_state', '生成依据已经变化，不能将旧建议绑定到新稿。')
        if tuple(sorted(source_versions)) != tuple(sorted(self.source_versions)):
            raise SnapshotMismatch('stale_state', '生成依据的来源版本或角色已经变化。')
