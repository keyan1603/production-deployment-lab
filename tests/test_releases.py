"""Offline tests for releases: manifests, lint, prompt patching, canary routing, the rollback rule, the admin API."""

import json
import threading

import httpx
import pytest

from a2a_layer.server import ServerThread, free_port
from deploylab.releases import (Guard, PROMPT_TARGETS, Registry, Release, ReleaseError, ReleaseSet, admin_routes, current_prompts, lint,
                                patched_prompts)
from deploylab.runtime import Lifecycle, make_app


def base(**over) -> Release:
    data = dict(id="r1", prompts=current_prompts(), models={"light": "gemini-3.5-flash-lite", "heavy": "gemini-3.5-flash"}, params={})
    data.update(over)
    return Release(**data)


# ---- manifests ----------------------------------------------------------------------------------
def test_digest_follows_behaviour_not_names():
    a, b = base(id="r1"), base(id="r2", parent="r1", notes="same content, different name")
    assert a.digest == b.digest
    changed = base(prompts={**current_prompts(), "responder": "Be rude."})
    assert changed.digest != a.digest
    assert base(models={"light": "other", "heavy": "gemini-3.5-flash"}).digest != a.digest


def test_registry_round_trip_and_tamper_detection(tmp_path):
    reg = Registry(tmp_path)
    rel = base()
    reg.save(rel)
    assert reg.get("r1") == rel and reg.ids() == ["r1"]
    path = reg.path("r1")
    data = json.loads(path.read_text())
    data["prompts"]["responder"] = "edited by hand after release"
    path.write_text(json.dumps(data))
    with pytest.raises(ReleaseError, match="digest mismatch"):
        reg.get("r1")                                          # a released manifest cannot be edited in place


@pytest.mark.parametrize("bad", ["../etc/passwd", "R1", "a b", "", "x" * 80, "-start"])
def test_release_ids_cannot_escape_the_directory(tmp_path, bad):
    with pytest.raises(ReleaseError):
        Registry(tmp_path).path(bad)


def test_unknown_manifest_fields_are_rejected():
    with pytest.raises(ReleaseError, match="unknown manifest fields"):
        Release.from_json({**base().to_json(), "temperature_override": 2.0})


# ---- lint ---------------------------------------------------------------------------------------
def test_lint_passes_the_shipped_prompts():
    assert lint(base()) == []


def test_lint_catches_the_silly_mistakes_before_any_model_is_called():
    prompts = dict(current_prompts())
    prompts["responder"] = ""
    prompts["tirage"] = "typo in the name"                      # would be silently ignored without the check
    prompts["triage"] = "x" * 5000
    prompts["resolver"] = "Use key AIza" + "a" * 30
    prompts["policy_addendum"] = "ask the knowledge base"
    del prompts["answerer"]
    problems = lint(base(prompts=prompts, models={"light": "Gemini Flash!", "heavy": ""}))
    text = " | ".join(problems)
    for expected in ("responder is empty", "unknown prompt name 'tirage'", "5000 characters", "credential", "ask_policy",
                     "missing prompt: answerer", "not a plausible model id", "missing model pin: heavy"):
        assert expected in text, (expected, text)


# ---- prompt patching ----------------------------------------------------------------------------
def test_patched_prompts_apply_only_inside_the_block_and_are_restored():
    import tickets.platform as tp
    original = tp.RESPONDER_PROMPT
    with patched_prompts({"responder": "PATCHED"}):
        assert tp.RESPONDER_PROMPT == "PATCHED"
    assert tp.RESPONDER_PROMPT == original


def test_patch_is_restored_even_when_the_build_fails():
    import tickets.platform as tp
    original = tp.TRIAGE_PROMPT
    with pytest.raises(RuntimeError):
        with patched_prompts({"triage": "PATCHED"}):
            raise RuntimeError("build failed")
    assert tp.TRIAGE_PROMPT == original


def test_two_releases_built_back_to_back_keep_their_own_prompt():
    import tickets.platform as tp
    built = []
    for text in ("ONE", "TWO"):
        with patched_prompts({"responder": text}):
            built.append(tp.RESPONDER_PROMPT)           # what a component built here would capture
    assert built == ["ONE", "TWO"] and tp.RESPONDER_PROMPT not in built


def test_concurrent_builds_never_see_each_others_prompt():
    import tickets.platform as tp
    seen, errors = {}, []

    def build(name):
        try:
            with patched_prompts({"responder": name}):
                for _ in range(200):
                    assert tp.RESPONDER_PROMPT == name
                seen[name] = True
        except AssertionError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=build, args=(f"R{i}",)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(seen) == 8


def test_every_patch_target_exists_in_the_system_under_deployment():
    assert set(current_prompts()) == set(PROMPT_TARGETS) and all(current_prompts().values())


# ---- routing ------------------------------------------------------------------------------------
def make_set(guard=None) -> ReleaseSet:
    rs = ReleaseSet("tickets", guard)
    for rid in ("r1", "r2", "r3"):
        rs.add(base(id=rid, prompts={**current_prompts(), "responder": f"prompt {rid}"}), component=f"platform-{rid}")
    return rs


def test_first_release_added_becomes_active_and_requests_go_to_it():
    rs = make_set()
    assert rs.active == "r1" and rs.choose("T1") == ("r1", "active")


def test_canary_share_is_close_to_the_requested_percent_and_stable_per_key():
    rs = make_set()
    rs.set_canary("r2", 20)
    keys = [f"ticket-{i}" for i in range(5000)]
    first = [rs.choose(k) for k in keys]
    assert first == [rs.choose(k) for k in keys]              # the same ticket always lands on the same side
    share = sum(1 for _, role in first if role == "canary") / len(keys)
    assert 0.17 < share < 0.23, share
    assert {role for _, role in first} == {"canary", "control"}


def test_canary_rules_are_enforced():
    rs = make_set()
    with pytest.raises(ReleaseError, match="different release"):
        rs.set_canary("r1", 10)
    with pytest.raises(ReleaseError, match="at most 50"):
        rs.set_canary("r2", 80)
    with pytest.raises(ReleaseError, match="not loaded"):
        rs.set_canary("r9", 10)


# ---- the rollback rule --------------------------------------------------------------------------
def feed(rs, role, release, outcomes):
    fired = None
    for o in outcomes:
        fired = rs.record(release, role, o, 1.0) or fired
    return fired


def test_a_healthy_canary_is_left_alone():
    rs = make_set()
    rs.set_canary("r2", 30)
    feed(rs, "control", "r1", ["released"] * 10 + ["held"])
    assert feed(rs, "canary", "r2", ["released"] * 9 + ["held"]) is None
    assert rs.canary is not None


def test_a_sick_canary_is_rolled_back_automatically_and_the_reason_is_recorded():
    rs = make_set()
    rs.set_canary("r2", 30)
    feed(rs, "control", "r1", ["released"] * 8)
    fired = feed(rs, "canary", "r2", ["held"] * 6)
    assert fired and fired["type"] == "auto_rollback" and fired["release"] == "r2" and fired["restored"] == "r1"
    assert "100%" in fired["reason"] and "0%" in fired["reason"]
    assert rs.canary is None and rs.active == "r1"
    assert rs.choose("any")[1] == "active"


def test_the_rule_waits_for_enough_canary_requests():
    rs = make_set(Guard(min_requests=6))
    rs.set_canary("r2", 30)
    feed(rs, "control", "r1", ["released"] * 8)
    assert feed(rs, "canary", "r2", ["held"] * 5) is None       # 5 of 5 bad is still too small a sample
    assert rs.canary is not None
    assert feed(rs, "canary", "r2", ["held"]) is not None


def test_if_control_is_just_as_bad_the_canary_is_not_blamed():
    rs = make_set()
    rs.set_canary("r2", 30)
    feed(rs, "control", "r1", ["held"] * 8)                      # an upstream problem hurting everyone
    assert feed(rs, "canary", "r2", ["held"] * 8) is None


def test_errors_count_as_bad_outcomes():
    rs = make_set()
    rs.set_canary("r2", 30)
    feed(rs, "control", "r1", ["released"] * 6)
    assert feed(rs, "canary", "r2", ["error"] * 6) is not None


# ---- operator actions ---------------------------------------------------------------------------
def test_activate_then_rollback_restores_the_previous_release():
    rs = make_set()
    rs.activate("r2")
    rs.activate("r3")
    assert rs.rollback() == "r2" and rs.rollback() == "r1"
    with pytest.raises(ReleaseError, match="nothing to roll back"):
        rs.rollback()


def test_rollback_during_a_canary_ends_the_canary_and_keeps_active():
    rs = make_set()
    rs.set_canary("r2", 10)
    assert rs.rollback() == "r1" and rs.canary is None and rs.active == "r1"


def test_activating_clears_the_canary():
    rs = make_set()
    rs.set_canary("r2", 10)
    rs.activate("r2")
    assert rs.canary is None and rs.active == "r2" and rs.status()["previous"] == "r1"


def test_shadow_statistics_accumulate_and_reset():
    rs = make_set()
    rs.set_shadow("r2")
    rs.record_shadow(True, True, False, False)
    rs.record_shadow(True, False, False, True)
    assert rs.shadow_stats == {"n": 2, "same_action": 2, "same_category": 1, "same_severity": 0, "candidate_errors": 1}
    rs.set_shadow(None)
    assert rs.shadow is None and rs.shadow_stats["n"] == 0


# ---- admin API ----------------------------------------------------------------------------------
@pytest.fixture
def admin(tmp_path):
    reg = Registry(tmp_path)
    for rid in ("r1", "r2"):
        reg.save(base(id=rid, prompts={**current_prompts(), "responder": f"prompt {rid}"}))
    reg.save(base(id="bad", prompts={**current_prompts(), "responder": ""}))
    rs = ReleaseSet("demo")
    rs.add(reg.get("r1"), "platform-r1")
    app = make_app(Lifecycle("demo", "1"), admin_routes(rs, reg, lambda rel: f"platform-{rel.id}", "k" * 20))
    srv = ServerThread(app, free_port()).start()
    yield srv, rs
    srv.stop()


H = {"X-Admin-Key": "k" * 20}


def test_admin_requires_the_key(admin):
    srv, _ = admin
    assert httpx.get(f"{srv.url}/admin/releases").status_code == 401
    assert httpx.get(f"{srv.url}/admin/releases", headers={"X-Admin-Key": "wrong" * 5}).status_code == 401
    assert httpx.post(f"{srv.url}/admin/releases/rollback", json={}).status_code == 401
    assert httpx.get(f"{srv.url}/admin/releases", headers=H).json()["active"] == "r1"


def test_load_lints_then_activate_canary_and_rollback_work_over_http(admin):
    srv, rs = admin
    post = lambda path, body: httpx.post(f"{srv.url}/admin/releases/{path}", json=body, headers=H)
    assert post("load", {"id": "bad"}).status_code == 409 and "failed lint" in post("load", {"id": "bad"}).json()["detail"]
    assert post("load", {"id": "nope"}).status_code == 409
    assert post("load", {"id": "r2"}).json()["loaded"] == "r2"
    assert post("canary", {"id": "r2", "percent": 25}).json()["canary"] == {"release": "r2", "percent": 25.0}
    assert post("activate", {"id": "r2"}).json()["active"] == "r2"
    assert post("rollback", {}).json()["active"] == "r1"
    assert post("rollback", {}).status_code == 409                # nothing left to roll back to
