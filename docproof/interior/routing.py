"""Luna proposes simple edits; Astra handles the bounded escalations."""
from copy import deepcopy
from pathlib import Path

from .astra import AstraReviewer, InteriorAstraError, _materialize, _validate_plan
from .verify import VerificationError, prepare_edits, validate_plan


def _checked(plan, packet, snapshot):
    _validate_plan(plan, packet, snapshot)
    validate_plan(plan, packet)
    prepare_edits(snapshot, plan['edits'])
    return plan


def _complex(instruction, edits):
    if instruction['disposition'] in {'clarification', 'designer'}:
        return True
    for eid in instruction['edit_ids']:
        edit = edits[eid]
        if (edit['font_style'] or edit['style_ranges'] or edit['expected_count'] != 1
                or any(c in edit['find'] + edit['replacement'] for c in '\r\n\u2028\u2029')):
            return True
    return False


def _subset(packet, instructions):
    """Keep full source context, but only assign the escalated evidence units."""
    evidence_ids = {eid for row in instructions for eid in row['covered_evidence_ids']}
    source_ids = {sid for row in instructions for sid in row['source_ids']}
    subset = deepcopy(packet)
    subset['sources'] = [row for row in subset['sources'] if row['id'] in source_ids]
    subset['evidence'] = [row for row in subset.get('evidence', []) if row['id'] in evidence_ids]
    for row in subset['sources']:
        for key in ('evidence_ids', 'required_evidence_ids'):
            if key in row:
                row[key] = [eid for eid in row[key] if eid in evidence_ids]
    for key in ('evidence_ids', 'required_evidence_ids'):
        if key in subset:
            subset[key] = sorted(evidence_ids)
    if 'source_ids' in subset:
        subset['source_ids'] = sorted(source_ids)
    if subset.get('text_source_id') not in source_ids:
        subset.pop('text_source_id', None)
        for key in ('text', 'text_raw'):
            subset.pop(key, None)
    # These duplicate intake indexes describe the complete packet, not this
    # assignment. Original source content remains available in sources.
    subset.pop('attachments', None)
    subset.pop('coverage', None)
    subset['assignment'] = {'evidence_ids': sorted(evidence_ids),
                            'note': 'Only these evidence IDs are assigned. Other source content is context.'}
    return subset


def _prefix(plan, prefix):
    result = deepcopy(plan)
    for row in result['instructions']:
        row['id'] = prefix + row['id']
        row['edit_ids'] = [prefix + eid for eid in row['edit_ids']]
    for row in result['edits']:
        row['id'] = prefix + row['id']
    return result


class LunaFirstReviewer(AstraReviewer):
    """At most one Luna plan and one Astra escalation; final review stays Astra.

    Operational failures (auth, quota, timeout or cancellation) stop the job.
    Only a completed proposal that fails local editorial validation is routed
    to a separate Astra planning stage. Neither model can bypass native checks.
    """
    def plan(self, packet, snapshot, work_dir, rules=None):
        root = Path(work_dir)
        receipt = {'version': 1, 'planner': 'gpt-5.6-luna', 'planner_effort': 'medium',
                   'escalation_model': 'gpt-6-astra', 'review_model': 'gpt-6-astra',
                   'escalated_evidence_ids': [], 'full_escalation': False}
        luna = AstraReviewer(planning_model='gpt-5.6-luna', simple_only=True)
        try:
            draft = _checked(luna.plan(packet, snapshot, root, rules=rules), packet, snapshot)
        except (InteriorAstraError, VerificationError) as exc:
            receipt.update(full_escalation=True, reason=str(exc))
            _materialize(root, 'model-routing.json', receipt)
            result = _checked(AstraReviewer().plan(packet, snapshot, root, rules=rules), packet, snapshot)
            return result
        _materialize(root, 'luna-plan.json', draft)
        edits = {row['id']: row for row in draft['edits']}
        escalated = [row for row in draft['instructions'] if _complex(row, edits)]
        # A model may leave a formatting/designer instruction unassigned after
        # another Luna instruction claimed the same submitted evidence unit.
        # Escalating that row alone gives Astra the whole source but no
        # evidence ownership, which invites it to repeat Luna's accepted edit.
        # Escalate the complete source-owned group in that case so one planner
        # owns every instruction attached to the shared evidence.
        unassigned_sources = {
            source for row in escalated if not row['covered_evidence_ids']
            for source in row['source_ids']
        }
        if unassigned_sources:
            original_ids = {row['id'] for row in escalated}
            escalated = [row for row in draft['instructions']
                         if row['id'] in original_ids
                         or set(row['source_ids']) & unassigned_sources]
        if not escalated:
            _materialize(root, 'model-routing.json', receipt)
            return draft
        subset = _subset(packet, escalated)
        receipt['escalated_evidence_ids'] = subset['assignment']['evidence_ids']
        _materialize(root, 'model-routing.json', receipt)
        note = ('Review only the evidence IDs in packet.assignment. The packet retains complete original '
                'source content for context; entries outside that assignment already have separate proposals. '
                'Do not add instructions or edits for unassigned evidence. Independently interpret the assigned '
                'original content, including formatting and strike-through marks. If the native edit schema '
                'cannot express an intended layout change, retain an explicit designer disposition.')
        resolved = _checked(AstraReviewer(planning_note=note).plan(subset, snapshot, root, rules=rules), subset, snapshot)
        _materialize(root, 'astra-escalation-plan.json', resolved)
        retained_ids = {row['id'] for row in escalated}
        retained = deepcopy(draft)
        retained['instructions'] = [row for row in retained['instructions'] if row['id'] not in retained_ids]
        keep_edits = {eid for row in retained['instructions'] for eid in row['edit_ids']}
        retained['edits'] = [row for row in retained['edits'] if row['id'] in keep_edits]
        retained, resolved = _prefix(retained, 'luna-'), _prefix(resolved, 'astra-')
        result = {'instructions': retained['instructions'] + resolved['instructions'],
                  'edits': retained['edits'] + resolved['edits'],
                  'questions': resolved['questions'], 'designer_reasons': resolved['designer_reasons']}
        return _checked(result, packet, snapshot)
