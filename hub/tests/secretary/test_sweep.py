"""The hub sweep: the docs folder (changelog, design index, decisions), memory notes and git, read into
hub_entries, open loops and hygiene checks. Settings that turn sources off are honoured."""

from __future__ import annotations

from conftest import ingest_all


def _loops():
    from app.secretary import views

    return views.all_loops()


def test_sweep_reads_docs_memory_and_git(loaded):
    from app.secretary import gitlog, store

    res = loaded.sweep
    assert res["repos"] == 4  # shop, tools, lib and the docs folder; the shop worktree is not a second repo
    assert {r["key"] for r in gitlog.repos()} == {"shop", "tools", "lib", "notes"}
    assert res["entries"] >= 5 and res["designs"] == 2 and res["decisions"] >= 1
    kinds = {r["kind"] for r in store.q("select distinct kind from hub_entries")}
    assert {"changelog", "followup", "design", "decision", "memory", "commit", "branch"} <= kinds
    texts = [lp["text"] for lp in _loops()]
    assert any(t.startswith("Push main") for t in texts)
    assert not any(t.strip(". ").lower() == "none" for t in texts)
    assert not any("Old idea" in t for t in texts)  # struck through
    by_kind = {lp["kind"] for lp in _loops()}
    assert {"followup", "design_status", "memory", "branch"} <= by_kind
    branch = next(lp for lp in _loops() if lp["kind"] == "branch")
    assert "feature/tax" in branch["text"] and branch["repo"] == "shop"


def test_hygiene_rules(loaded):
    from app.secretary import views

    checks = views.hygiene()["checks"]
    assert any(c["rule"] == "commits_without_changelog" and c["repo"] == "tools" for c in checks)
    assert any(c["rule"] == "memory_not_pushed" for c in checks)
    for c in checks:
        assert "/home/alex" not in c["text"]  # texts use labels, paths stay in src


def test_sweep_twice_keeps_loop_ids_and_states(loaded):
    from app.secretary import sweep, views

    before = {lp["id"]: lp for lp in _loops()}
    first = next(iter(before))
    views.loop_action(first, "done")
    res = sweep.run_sweep()
    after = {lp["id"]: lp for lp in _loops()}
    assert set(after) == set(before)
    assert after[first]["state"] == "done"
    assert res["loops"]["gone"] == 0


def test_sources_can_be_turned_off(world, monkeypatch):
    from app import config
    from app.secretary import gitlog, store, sweep

    monkeypatch.setattr(config, "READ_GIT", False)
    monkeypatch.setattr(config, "READ_MEMORY", False)
    gitlog.forget_cache()
    ingest_all(world)
    sweep.run_sweep()
    kinds = {r["kind"] for r in store.q("select distinct kind from hub_entries")}
    assert "memory" not in kinds and "branch" not in kinds and "commit" not in kinds
    assert not [lp for lp in _loops() if lp["kind"] in ("memory", "branch")]


def test_no_docs_folder_sweeps_git_and_memory(world, monkeypatch):
    from app import config
    from app.secretary import store, sweep

    monkeypatch.setattr(config, "DOCS_DIR", "")
    ingest_all(world)
    res = sweep.run_sweep()
    kinds = {r["kind"] for r in store.q("select distinct kind from hub_entries")}
    assert "changelog" not in kinds and "design" not in kinds
    assert "branch" in kinds and "memory" in kinds
    assert res["entries"] == 0


def test_docs_folder_missing_on_disk(world, monkeypatch):
    """DOCS_DIR set but not there (unmounted, renamed): the sweep still runs."""
    from app import config
    from app.secretary import sweep, views

    monkeypatch.setattr(config, "DOCS_DIR", str(world.root / "no-such-folder"))
    res = sweep.run_sweep()
    assert res["entries"] == 0
    assert views.sources()["docs"]["found"] is False


def test_design_status_is_read_from_its_first_sentence():
    """A long status line names parts of the work later on; the first sentence says where the feature stands."""
    from app.secretary import hubdocs

    lane = hubdocs.lane_of("in progress (2026-10-04). M0–M3 built (`abc1234`). The wizard built and tested.")
    assert (lane["lane"], lane["short"]) == ("building", "In progress 4 Oct")
    lane = hubdocs.lane_of("implemented + deployed 2026-10-04 (`main` @ `1234abc`): the cart page. 2026-10-04: a source "
                           "built on `feature/x`, not merged.")
    assert (lane["lane"], lane["short"]) == ("shipped", "Deployed 4 Oct")
    lane = hubdocs.lane_of("**v1 feature-complete on branches (2026-09-25), not deployed.** More text.")
    assert (lane["lane"], lane["short"]) == ("built", "Feature-complete 25 Sep, not deployed")
    assert hubdocs.lane_of("See the notes. Built 2026-09-21, cutover pending.")["lane"] == "built"  # no stage word first


def test_markdown_is_stripped_but_code_spans_keep_their_text():
    from app.secretary import hubdocs

    assert hubdocs.strip_md("A hook (matcher `mcp__shop__.*`) on **every** call") == "A hook (matcher mcp__shop__.*) on every call"
    assert hubdocs.strip_md("[the doc](design/x.md) and __bold__") == "the doc and bold"


def test_a_followup_that_says_there_is_nothing_to_do_is_no_loop():
    from app.secretary import hubdocs

    def none(text):
        return hubdocs.followup_item({"line": 1, "text": text})["none"]

    assert none("None.") and none("Cross-project: none. The API is only read.") and none("**Cross-project:**")
    assert not none("Cross-project: deploy the admin page after the API.")
    assert not none("Rotate the staging SMTP password.")


def test_decision_text_keeps_code_names(tmp_path):
    """The decision cell is made plain once: `mcp__shop__.*` keeps its underscores."""
    from app.secretary import hubdocs

    doc = tmp_path / "00-overview.md"
    doc.write_text("# Shop\n\n## Decisions\n\n| Date | Decision | Why |\n|---|---|---|\n"
                   "| 2026-10-04 | **D12** A hook (matcher `mcp__shop__.*`) posts `session_id` | **Decided** |\n")
    decs = hubdocs._parse_design_doc(doc)["decisions"]
    assert decs and decs[0]["text"] == "A hook (matcher mcp__shop__.*) posts session_id", decs
    assert decs[0]["ref"] == "D12" and decs[0]["outcome"] == "Decided"
