"""The frontier chapter sweep: windowing, fail-closed anchoring, the confirm
valve hand-off, and the pipeline's fail-open gating."""
from docproof.chaptersweep import SWEEP_SCHEMA, propose, windows
from docproof.config import Config, load_config
from docproof.models import DocumentModel, ParagraphRef, Usage
from docproof.providers import ProviderResult


def _para(pid, text, reviewable=True):
    return ParagraphRef(pid, "word/document.xml", "body", text, "Normal",
                        reviewable)


class _SweepProvider:
    """Returns canned parsed findings per window, recording each payload."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete_structured(self, *, model, system, user, schema, schema_name,
                            max_tokens):
        self.calls.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            return ProviderResult(stop_reason="error", error=str(reply))
        return ProviderResult(parsed=reply)


def test_windows_pack_in_order_with_global_numbering():
    paras = [_para(f"p{i}", f"Paragraph {i} text. " * 30) for i in range(6)]
    paras.insert(2, _para("skip", "", True))              # empty: skipped
    paras.insert(3, _para("art", "not reviewable", False))  # skipped
    packed = windows(paras, max_chars=1200)
    assert len(packed) > 1
    numbers = [n for rows in packed for n, _ in rows]
    assert numbers == list(range(1, len(numbers) + 1))    # global, gapless
    ids = [p.para_id for rows in packed for _, p in rows]
    assert "skip" not in ids and "art" not in ids


def test_propose_anchors_verbatim_and_fails_closed():
    paras = [_para("p1", "She left there bags on the platform."),
             _para("p2", "It was to late for apologies.")]
    provider = _SweepProvider([{
        "findings": [
            {"para": 1, "quote": "there bags", "correction": "their bags",
             "note": "their/there"},
            {"para": 2, "quote": "to late", "correction": "too late",
             "note": "to/too"},
            {"para": 2, "quote": "not in the text", "correction": "x",
             "note": "hallucinated"},            # unlocatable -> dropped
            {"para": 9, "quote": "to late", "correction": "too late",
             "note": "bad para"},                # unknown para -> dropped
            {"para": 1, "quote": "platform", "correction": "platform",
             "note": "noop"},                    # no change -> dropped
        ]}])
    usage = Usage()
    stats = {}
    cands = propose(paras, provider, model="claude-fable-5",
                    max_output_tokens=1000, usage=usage, window_chars=48_000,
                    stats=stats)
    assert [(c.para_id, c.original, c.replacement) for c in cands] == [
        ("p1", "there bags", "their bags"), ("p2", "to late", "too late")]
    # Anchors are real spans in the paragraph text.
    assert paras[0].text[cands[0].start:cands[0].end] == "there bags"
    assert stats["unlocated"] == 1 and stats["unknown_para"] == 1
    assert stats["noop"] == 1 and stats["candidates"] == 2
    assert usage.api_calls == 1


def test_failed_window_is_skipped_not_fatal():
    paras = [_para("p1", "One window. " * 200),
             _para("p2", "Two window. " * 200)]
    provider = _SweepProvider([
        RuntimeError("boom"),
        {"findings": [{"para": 2, "quote": "Two window.",
                       "correction": "Second window.", "note": "n"}]}])
    stats = {}
    cands = propose(paras, provider, model="m", max_output_tokens=100,
                    usage=Usage(), window_chars=2_000, stats=stats)
    assert stats["windows"] == 2 and stats["window_failed"] == 1
    assert len(cands) == 1 and cands[0].para_id == "p2"


def test_schema_is_strict():
    assert SWEEP_SCHEMA["additionalProperties"] is False
    item = SWEEP_SCHEMA["properties"]["findings"]["items"]
    assert set(item["required"]) == {"para", "quote", "correction", "note"}


def test_config_defaults_off_and_wired():
    cfg = Config()
    assert cfg.chapter_sweep.enabled is False
    assert cfg.chapter_sweep.model == "claude-fable-5"
    loaded = load_config("config/default.yaml")
    assert loaded.chapter_sweep.enabled is False
    assert loaded.chapter_sweep.effort == "xhigh"


def test_feature_switch_and_profiles_exclude_it():
    from app.features import FEATURES_BY_ID
    from docproof.profiles import CANDIDATE_ONLY, DETECTOR_ONLY, apply_profile
    spec = FEATURES_BY_ID["chapter_sweep"]
    assert spec.heavy is True
    cfg = Config(chapter_sweep={"enabled": True})
    for profile in (CANDIDATE_ONLY, DETECTOR_ONLY):
        assert apply_profile(cfg.model_copy(deep=True),
                             profile).chapter_sweep.enabled is False


def test_pipeline_gate_skips_without_vendor_key(tmp_path, monkeypatch):
    # Enabled but the Anthropic key is missing: the sweep must be a coverage
    # note, never a failed review — and must call no provider.
    from docproof.pipeline import chapter_sweep_findings

    class _Prepared:
        whole_document = True
        doc = DocumentModel("b.docx", (_para("p1", "text"),))

    def factory(cfg):
        raise RuntimeError("No Claude API key found.")

    class _Coverage:
        notes = []
        def note(self, *a): self.notes.append(a)

    cfg = Config(chapter_sweep={"enabled": True})
    coverage = _Coverage()
    out = chapter_sweep_findings(cfg, _Prepared(), object(), Usage(), factory,
                                 coverage=coverage)
    assert out == []
    assert any("Chapter sweep" in n[0] for n in coverage.notes)
