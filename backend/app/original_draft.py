"""User-facing baseline from immutable import rows, never the edited current draft."""
from app.draft_actions import join_markdown


def read_original_draft(connection, session):
    sid, generation = session['session_id'], session['current_resume_generation']
    # User reordering can update old rows' positions. Creation order remains
    # the order in which this generation's import transaction wrote its blocks.
    blocks = connection.execute("""SELECT * FROM draft_blocks
        WHERE session_id=? AND resume_generation=? AND block_revision=1
          AND write_kind IN ('import','replacement') ORDER BY rowid""", (sid, generation)).fetchall()
    if not blocks:
        return None
    refs, materials, seen = [], [], set()
    for block in blocks:
        for ref in connection.execute("""SELECT r.*,s.attachment_id,s.role FROM draft_sources r
            JOIN source_blocks s ON s.session_id=r.session_id AND s.source_id=r.source_id AND s.source_revision=r.source_revision
            WHERE r.session_id=? AND r.block_id=? AND r.block_revision=1 ORDER BY r.rowid""", (sid, block['block_id'])):
            key = (ref['source_id'], ref['source_revision'], ref['quote'])
            if key not in seen:
                refs.append({'sourceId': key[0], 'sourceRevision': key[1], 'quote': key[2]})
                seen.add(key)
            if ref['role'] == 'resume' and ref['attachment_id'] and ref['attachment_id'] not in materials:
                materials.append(ref['attachment_id'])
    return {'sessionId': sid, 'resumeGeneration': generation, 'markdown': join_markdown(blocks),
            'sourceRefs': refs, 'materialIds': materials}
