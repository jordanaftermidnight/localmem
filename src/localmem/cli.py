"""LOCALMEM CLI — admin and debug operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import load_config
from .embedder import Embedder
from .graph_store import GraphStore
from .intelligence import IntelligenceEngine
from .logging_config import setup_logging
from .metadata_store import MetadataStore
from .vector_store import VectorStore


async def cmd_stats(args: argparse.Namespace) -> None:
    """Show storage statistics."""
    cfg = load_config(args.config)
    metadata = MetadataStore(cfg)
    await metadata.initialize()

    wings = await metadata.list_wings()
    rooms = await metadata.list_rooms()

    print("=== LOCALMEM Statistics ===\n")

    print(f"Wings: {len(wings)}")
    for w in wings:
        wing_rooms = [r for r in rooms if r["wing"] == w]
        total = sum(r["entry_count"] for r in wing_rooms)
        print(f"  {w}: {len(wing_rooms)} rooms, {total} entries")
        for r in wing_rooms:
            print(f"    {r['room']}: {r['entry_count']} entries (last: {r['last_written']})")

    # Graph stats
    graph = GraphStore(cfg)
    await graph.initialize()
    gstats = await graph.stats()
    print(f"\nGraph: {gstats['nodes']} nodes, {gstats['edges']} edges")
    print(f"  Density: {gstats['density']:.4f}")
    print(f"  Components: {gstats['weakly_connected_components']}")
    await graph.shutdown()

    # Vector store count
    embedder = Embedder(cfg)
    embedder.load()
    vs = VectorStore(cfg, embedder)
    await vs.initialize()
    count = await vs.count()
    print(f"\nVector store: {count} entries")


async def cmd_triples(args: argparse.Namespace) -> None:
    """Query or list triples."""
    cfg = load_config(args.config)
    metadata = MetadataStore(cfg)
    await metadata.initialize()

    triples = await metadata.query_triples(
        subject=args.subject,
        predicate=args.predicate,
        active_only=not args.all,
    )

    if not triples:
        print("No triples found.")
        return

    for t in triples:
        status = "active" if t.valid_to is None else f"superseded ({t.valid_to})"
        print(f"  ({t.subject}, {t.predicate}, {t.object}) [{status}]")
        print(f"    id={t.id} agent={t.source_agent} confidence={t.confidence}")


async def cmd_timeline(args: argparse.Namespace) -> None:
    """Show timeline for a subject+predicate."""
    cfg = load_config(args.config)
    metadata = MetadataStore(cfg)
    await metadata.initialize()

    triples = await metadata.triple_timeline(args.subject, args.predicate)
    if not triples:
        print(f"No history for {args.subject}.{args.predicate}")
        return

    print(f"Timeline: {args.subject}.{args.predicate}\n")
    for t in triples:
        end = t.valid_to or "now"
        print(f"  {t.valid_from} -> {end}: {t.object}")
        if t.superseded_by:
            print(f"    superseded by {t.superseded_by}")


async def cmd_diary(args: argparse.Namespace) -> None:
    """Read diary entries."""
    cfg = load_config(args.config)
    metadata = MetadataStore(cfg)
    await metadata.initialize()

    entries = await metadata.read_diary(
        agent_id=args.agent,
        limit=args.limit,
    )

    if not entries:
        print("No diary entries found.")
        return

    for e in entries:
        header = f"[{e.timestamp}] {e.agent_id}"
        if e.mood:
            header += f" ({e.mood})"
        print(header)
        print(f"  {e.content}")
        if e.tags:
            print(f"  tags: {', '.join(e.tags)}")
        print()


async def cmd_graph(args: argparse.Namespace) -> None:
    """Show graph patterns and stats."""
    cfg = load_config(args.config)
    graph = GraphStore(cfg)
    await graph.initialize()

    if args.patterns:
        patterns = await graph.get_patterns(min_frequency=args.min_freq)
        if not patterns:
            print("No patterns found.")
        else:
            print(f"Patterns (min frequency={args.min_freq}):\n")
            for p in patterns:
                print(f"  {p['name']}: freq={p['frequency']}, connections={p['connections']}")
                if p.get("first_seen"):
                    print(f"    first seen: {p['first_seen']}")
    else:
        stats = await graph.stats()
        print(json.dumps(stats, indent=2))

    await graph.shutdown()


async def cmd_rooms(args: argparse.Namespace) -> None:
    """List all rooms or rooms in a wing."""
    cfg = load_config(args.config)
    metadata = MetadataStore(cfg)
    await metadata.initialize()

    rooms = await metadata.list_rooms(wing=args.wing)
    if not rooms:
        print("No rooms registered.")
        return

    for r in rooms:
        print(f"  {r['wing']}:{r['room']} — {r['entry_count']} entries (last: {r['last_written']})")


async def _init_intelligence(cfg):
    """Initialize stores and return an IntelligenceEngine."""
    embedder = Embedder(cfg)
    embedder.load()
    vs = VectorStore(cfg, embedder)
    gs = GraphStore(cfg)
    ms = MetadataStore(cfg)
    await vs.initialize()
    await gs.initialize()
    await ms.initialize()
    engine = IntelligenceEngine(cfg, vs, ms, gs)
    return vs, gs, ms, engine


async def cmd_intelligence(args: argparse.Namespace) -> None:
    """Intelligence operations: detect, alerts, report."""
    cfg = load_config(args.config)

    if args.intel_cmd == "detect":
        vs, gs, ms, engine = await _init_intelligence(cfg)
        results = await engine.run_detection()
        stored = await engine.store_alerts(results)

        print("=== Intelligence Detection ===\n")
        seqs = results["tool_sequences"]
        if seqs:
            print(f"Tool Sequences: {len(seqs)} patterns")
            for s in seqs[:5]:
                arrow = " \u2192 ".join(s["sequence"])
                print(f"  {arrow}: {s['frequency']}x (success: {s['success_rate']*100:.0f}%)")
            print()

        prefs = results["provider_preferences"]
        if prefs:
            print(f"Provider Preferences: {len(prefs)} task types")
            for p in prefs[:5]:
                label = f"{p['preferred_provider']} ({p['share']*100:.0f}%)"
                print(f"  {p['task_type']}: {label} \u2190 {p['pattern']}")
            print()

        clusters = results["node_clusters"]
        if clusters:
            print(f"Node Clusters: {len(clusters)} clusters")
            for c in clusters[:5]:
                members = ", ".join(c["members"])
                print(f"  [{members}] cohesion={c['cohesion']}")
            print()

        corrs = results["cross_wing"]
        if corrs:
            print(f"Cross-Wing Correlations: {len(corrs)}")
            for r in corrs[:5]:
                pair = " \u2194 ".join(r["wings"])
                print(f"  {pair}: strength={r['strength']} ({r['co_occurrences']} co-occurrences)")
            print()

        if not any([seqs, prefs, clusters, corrs]):
            print("No patterns detected. More data needed.\n")

        print(f"Alerts stored: {stored}")
        await gs.shutdown()

    elif args.intel_cmd == "alerts":
        vs, gs, ms, engine = await _init_intelligence(cfg)
        alerts = await vs.scroll(
            wing="shared", room=cfg.intelligence.alert_room, limit=args.limit * 2,
        )
        if args.wing:
            alerts = [a for a in alerts if args.wing in a.tags]
        alerts = alerts[:args.limit]

        if not alerts:
            print("No intelligence alerts found.")
        else:
            print(f"=== Intelligence Alerts ({len(alerts)}) ===\n")
            for a in alerts:
                tags = ", ".join(t for t in a.tags if t != "intelligence")
                print(f"  [{a.created_at[:19]}] {a.content}")
                print(f"    tags: {tags}  importance: {a.importance}")
                print()
        await gs.shutdown()

    elif args.intel_cmd == "report":
        vs, gs, ms, engine = await _init_intelligence(cfg)
        results = await engine.run_detection()

        print("=== LOCALMEM Intelligence Report ===\n")

        # Patterns
        seqs = results["tool_sequences"]
        if seqs:
            print("Tool Sequences:")
            for s in seqs[:5]:
                arrow = " \u2192 ".join(s["sequence"])
                print(f"  {arrow}: {s['frequency']}x (success: {s['success_rate']*100:.0f}%)")
            print()

        prefs = results["provider_preferences"]
        if prefs:
            print("Provider Preferences:")
            for p in prefs[:5]:
                label = f"{p['preferred_provider']} ({p['share']*100:.0f}%)"
                print(f"  {p['task_type']}: {label} \u2190 {p['pattern']}")
            print()

        clusters = results["node_clusters"]
        if clusters:
            print("Node Clusters:")
            for c in clusters[:5]:
                members = ", ".join(c["members"])
                print(f"  [{members}] cohesion={c['cohesion']}")
            print()

        corrs = results["cross_wing"]
        if corrs:
            print("Cross-Wing Correlations:")
            for r in corrs[:5]:
                pair = " \u2194 ".join(r["wings"])
                print(f"  {pair}: strength={r['strength']} ({r['co_occurrences']} co-occurrences)")
            print()

        # Importance distribution
        print("Importance Distribution:")
        for w in cfg.wings:
            entries = await ms.get_top_entries(wing=w, limit=1000)
            if entries:
                scores = [e["effective_score"] for e in entries]
                avg_base = sum(e["score"] for e in entries) / len(entries)
                avg_eff = sum(scores) / len(scores)
                print(f"  {w}: {len(entries)} entries, avg score={avg_base:.2f}, decayed avg={avg_eff:.2f}")
        print()
        await gs.shutdown()

    else:
        print("Usage: localmem intelligence {detect|alerts|report}")


async def cmd_health(args: argparse.Namespace) -> None:
    """Check store connectivity and display health summary."""
    cfg = load_config(args.config)
    embedder_inst = Embedder(cfg)
    embedder_inst.load()

    print("=== LOCALMEM Health ===\n")

    # Vector store
    vs = VectorStore(cfg, embedder_inst)
    try:
        await vs.initialize()
        total = await vs.count()
        print(f"  Vector Store: ok ({total} entries)")
    except Exception as e:
        print(f"  Vector Store: ERROR — {e}")

    # Metadata store
    ms = MetadataStore(cfg)
    try:
        await ms.initialize()
        wings = await ms.list_wings()
        print(f"  Metadata Store: ok ({len(wings)} wings)")
    except Exception as e:
        print(f"  Metadata Store: ERROR — {e}")

    # Graph store
    gs = GraphStore(cfg)
    try:
        await gs.initialize()
        stats = await gs.stats()
        print(f"  Graph Store: ok ({stats['nodes']} nodes, {stats['edges']} edges)")
        await gs.shutdown()
    except Exception as e:
        print(f"  Graph Store: ERROR — {e}")

    # Embedding
    print(
        f"  Embedding: {cfg.embedding.model} on {embedder_inst.resolved_device}"
        f" (sparse: {'yes' if embedder_inst.has_sparse else 'no'})"
    )
    print()


async def cmd_smoke(args: argparse.Namespace) -> None:
    """Run a full pipeline smoke test against a temporary data directory."""
    import tempfile
    import shutil

    from .intelligence import IntelligenceEngine
    from .models import Entry, EntryType, SearchQuery, Triple
    from .wake_up import WakeUp

    cfg_base = load_config(args.config)
    tmp = tempfile.mkdtemp(prefix="localmem_smoke_")
    passed = 0
    failed = 0

    def _report(name: str, ok: bool, detail: str = ""):
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  PASS  {name}{' — ' + detail if detail else ''}")
        else:
            failed += 1
            print(f"  FAIL  {name}{' — ' + detail if detail else ''}")

    print("=== LOCALMEM Smoke Test ===\n")

    # 1. Init stores
    from .config import LocalmemConfig, StorageConfig
    cfg = LocalmemConfig(
        storage=StorageConfig(
            base_path=tmp,
            qdrant_path=f"{tmp}/qdrant",
            sqlite_path=f"{tmp}/test.db",
            graph_path=f"{tmp}/graph.json",
        ),
        embedding=cfg_base.embedding,
    )

    try:
        embedder_inst = Embedder(cfg)
        embedder_inst.load()
        vs = VectorStore(cfg, embedder_inst)
        await vs.initialize()
        gs = GraphStore(cfg)
        await gs.initialize()
        ms = MetadataStore(cfg)
        await ms.initialize()
        _report("Init stores", True, f"device={embedder_inst.resolved_device}")
    except Exception as e:
        _report("Init stores", False, str(e))
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"\n{passed} passed, {failed} failed")
        sys.exit(1)

    smoke_wings = cfg.wings if cfg.wings else ["default"]
    primary_wing = smoke_wings[0]

    try:
        # 2. Store entries
        try:
            for wing in smoke_wings:
                entry = Entry(
                    wing=wing, room="smoke-test", agent_id=wing,
                    entry_type=EntryType.GENERIC,
                    content=f"Smoke test entry for {wing}", importance=0.6,
                    tags=["smoke-test"],
                )
                await vs.store(entry)
                await ms.register_room(wing, "smoke-test")
            _report("Store entries", True, f"{len(smoke_wings)} entries across {len(smoke_wings)} wings")
        except Exception as e:
            _report("Store entries", False, str(e))

        # 3. Search
        try:
            results = await vs.search(SearchQuery(query="smoke test", wing=primary_wing, limit=5))
            _report("Search", len(results) >= 1, f"{len(results)} results")
        except Exception as e:
            _report("Search", False, str(e))

        # 4. Retrieve
        try:
            count = await vs.count()
            _report("Count/retrieve", count >= len(smoke_wings), f"{count} total entries")
        except Exception as e:
            _report("Count/retrieve", False, str(e))

        # 5. Wake-up
        try:
            manifests_dir = f"{tmp}/manifests"
            Path(manifests_dir).mkdir()
            (Path(manifests_dir) / f"{primary_wing}.yaml").write_text(
                f"agent: {primary_wing}\nrole: smoke test\ncapabilities: []\nwake_rooms: []\n"
            )
            wake = WakeUp(cfg, vs, ms, manifests_dir=manifests_dir)
            ctx = await wake.wake(primary_wing)
            _report("Wake-up", ctx.agent_id == primary_wing, f"~{ctx.total_tokens_estimate} tokens")
        except Exception as e:
            _report("Wake-up", False, str(e))

        # 6. Graph operations
        try:
            await gs.add_node("smoke:a", {"type": "test"})
            await gs.add_node("smoke:b", {"type": "test"})
            await gs.add_edge("smoke:a", "smoke:b", {"relation": "test"})
            stats = await gs.stats()
            _report("Graph", stats["nodes"] >= 2, f"{stats['nodes']} nodes, {stats['edges']} edges")
        except Exception as e:
            _report("Graph", False, str(e))

        # 7. Knowledge triple
        try:
            triple = Triple(
                subject="smoke", predicate="status", object="passing",
                source_agent="smoke-test",
            )
            await ms.add_triple(triple)
            triples = await ms.query_triples(subject="smoke")
            _report("Knowledge triple", len(triples) >= 1, f"{len(triples)} triples")
        except Exception as e:
            _report("Knowledge triple", False, str(e))

        # 8. Intelligence
        try:
            engine = IntelligenceEngine(cfg, vs, ms, gs)
            results = await engine.run_detection()
            _report("Intelligence detect", isinstance(results, dict), "4 detectors ran")
        except Exception as e:
            _report("Intelligence detect", False, str(e))

    finally:
        await gs.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


async def cmd_init(args: argparse.Namespace) -> None:
    """Scaffold a working localmem.yaml + data directory.

    Writes a minimal config that runs out-of-the-box on a fresh machine.
    For the canonical layout the README suggests, just run it with no args
    from the directory you want as your data root.
    """
    data_dir = Path(args.data_dir).expanduser().resolve()
    cfg_path = data_dir / "localmem.yaml"

    if cfg_path.exists() and not args.force:
        print(f"ERROR: {cfg_path} already exists — pass --force to overwrite",
              file=sys.stderr)
        sys.exit(1)

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "data").mkdir(exist_ok=True)

    wings = args.wing or ["assistant"]
    wing_lines = "\n".join(f"  - {w}" for w in wings)

    qdrant_block = (
        f'  qdrant_mode: "server"\n'
        f'  qdrant_url: "{args.qdrant_url}"\n'
        f'  qdrant_path: "./data/qdrant"  # ignored in server mode'
        if args.qdrant_mode == "server"
        else
        '  qdrant_mode: "local"\n'
        '  qdrant_path: "./data/qdrant"'
    )

    dashboard_block = (
        "dashboard:\n"
        "  enabled: true\n"
        '  host: "127.0.0.1"\n'
        "  port: 8782\n"
        "  cors_origins:\n"
        '    - "http://localhost:8785"\n'
        if args.dashboard
        else ""
    )

    content = (
        "# localmem config — generated by `localmem init`.\n"
        "# See https://github.com/jordanaftermidnight/localmem for the full schema.\n"
        "\n"
        f"wings:\n{wing_lines}\n"
        "\n"
        "storage:\n"
        '  base_path: "./data"\n'
        f"{qdrant_block}\n"
        '  sqlite_path: "./data/localmem.db"\n'
        '  graph_path: "./data/graph.json"\n'
        "\n"
        "embedding:\n"
        '  model: "all-MiniLM-L6-v2"\n'
        '  device: "cpu"\n'
        + (("\n" + dashboard_block) if dashboard_block else "")
    )
    cfg_path.write_text(content)

    print(f"  wrote {cfg_path}")
    print(f"  data dir: {data_dir / 'data'}/")
    print()
    print("Next:")
    print(f"  cd {data_dir}")
    if args.qdrant_mode == "server":
        print(f"  # ensure Qdrant is reachable at {args.qdrant_url}")
    print(f"  localmem -c localmem.yaml serve")
    if args.dashboard:
        print(f"  # in another terminal:")
        print(f"  localmem -c localmem.yaml dashboard")


async def cmd_serve(args: argparse.Namespace) -> None:
    """Start the MCP server."""
    from .mcp_server import run_async
    await run_async(args.config)


async def cmd_dashboard(args: argparse.Namespace) -> None:
    """Start the dashboard REST+WS server (localhost:8782)."""
    try:
        from .api.app import serve as dashboard_serve
    except ImportError as e:
        print(f"Dashboard deps missing: {e}")
        print("Install: pip install -e '.[dashboard]'")
        sys.exit(1)
    cfg = load_config(args.config)
    await dashboard_serve(cfg)


async def _set_pinned(cfg, entry_id: str, pinned: bool) -> bool:
    """Set pinned flag on both metadata store and Qdrant payload. Returns
    True if the entry exists in Qdrant, False otherwise."""
    embedder_inst = Embedder(cfg)
    embedder_inst.load()
    vs = VectorStore(cfg, embedder_inst)
    await vs.initialize()
    ms = MetadataStore(cfg)
    await ms.initialize()

    entry = await vs.retrieve(entry_id)
    if entry is None:
        return False

    await ms.set_pinned(entry_id, pinned, wing=entry.wing)
    await vs.set_pinned(entry_id, pinned)
    return True


async def cmd_pin(args: argparse.Namespace) -> None:
    """Pin an entry — exempt from retention/consolidation/archive."""
    cfg = load_config(args.config)
    found = await _set_pinned(cfg, args.entry_id, True)
    if not found:
        print(f"Entry not found: {args.entry_id}")
        sys.exit(1)
    print(f"Pinned: {args.entry_id}")


async def cmd_unpin(args: argparse.Namespace) -> None:
    """Unpin an entry — re-enters normal retention pipeline."""
    cfg = load_config(args.config)
    found = await _set_pinned(cfg, args.entry_id, False)
    if not found:
        print(f"Entry not found: {args.entry_id}")
        sys.exit(1)
    print(f"Unpinned: {args.entry_id}")


async def cmd_list_pinned(args: argparse.Namespace) -> None:
    """List currently pinned entries."""
    cfg = load_config(args.config)
    ms = MetadataStore(cfg)
    await ms.initialize()
    rows = await ms.list_pinned(wing=args.wing, limit=args.limit)
    if not rows:
        print("No pinned entries.")
        return
    print(f"=== Pinned entries ({len(rows)}) ===\n")
    for r in rows:
        print(f"  {r['entry_id']}  wing={r['wing']}  score={r['base_score']}")
        if r.get("last_accessed"):
            print(f"    last accessed: {r['last_accessed']}")


def _server_running(cfg) -> bool:
    """Quick check: is the dashboard server (which holds the Qdrant lock) up?
    Treats any 2xx/4xx response as 'up' — we just need to know if the port
    is bound by something that speaks HTTP, regardless of auth."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect((cfg.dashboard.host, cfg.dashboard.port))
            return True
    except (OSError, ConnectionRefusedError):
        return False


async def cmd_prune(args: argparse.Namespace) -> None:
    """Run the retention/consolidation sweep. Without --apply, runs in dry-run
    mode and reports candidates without modifying any store."""
    from .consolidator import Consolidator

    cfg = load_config(args.config)
    if not cfg.retention.enabled:
        print("Retention is disabled (set retention.enabled=true in localmem.yaml).")
        sys.exit(1)

    if args.apply and _server_running(cfg):
        print(
            f"WARNING: Dashboard/MCP server appears to be running on "
            f"{cfg.dashboard.host}:{cfg.dashboard.port}."
        )
        print(
            "Qdrant local doesn't support concurrent writers across processes."
        )
        print(
            "Either stop the server first, or trigger consolidation via "
            "POST /api/prune/run (Phase 4)."
        )
        sys.exit(2)

    embedder_inst = Embedder(cfg)
    embedder_inst.load()
    vs = VectorStore(cfg, embedder_inst)
    await vs.initialize()
    ms = MetadataStore(cfg)
    await ms.initialize()
    gs = GraphStore(cfg)
    await gs.initialize()

    consolidator = Consolidator(cfg, vs, ms, graph_store=gs)
    wings = [args.wing] if args.wing else None
    report = await consolidator.consolidate_all(dry_run=not args.apply, wings=wings)

    mode = "DRY-RUN" if report.dry_run else "APPLIED"
    print(f"=== Retention sweep [{mode}] ===")
    print(f"started:  {report.started_at}")
    print(f"finished: {report.finished_at}")
    if not report.dry_run:
        print(f"orphans reconciled: {report.orphans_reconciled}")
    print()

    for wr in report.wings:
        print(f"[{wr.wing}]")
        if wr.error:
            print(f"  error: {wr.error}")
            continue
        print(f"  candidates:           {wr.candidates}")
        print(f"  consolidated groups:  {wr.consolidated_groups}")
        print(f"  consolidated entries: {wr.consolidated_entries}")
        print(f"  skipped groups:       {wr.skipped_groups}")
        for g in wr.groups[:10]:
            wing, room, week = g.group_key
            tag = "WOULD-CONSOLIDATE" if report.dry_run and g.summary_id is None and not g.skipped_reason else ""
            tag = tag or ("OK" if g.summary_id else f"SKIP ({g.skipped_reason})")
            print(f"    {wing}/{room} {week}  {g.entry_count:>4}  {tag}")
        if len(wr.groups) > 10:
            print(f"    … and {len(wr.groups) - 10} more groups")
        print()

    await gs.shutdown()


async def cmd_archive(args: argparse.Namespace) -> None:
    """Archive operations: write (cold-tier transition), sql, search, restore, stats."""
    from .archiver import Archiver

    cfg = load_config(args.config)
    sub_cmd = args.archive_cmd

    if sub_cmd == "write":
        if not cfg.retention.enabled:
            print("Retention is disabled — nothing to archive.")
            sys.exit(1)
        if args.apply and _server_running(cfg):
            print(
                f"WARNING: server appears to be running on {cfg.dashboard.host}:{cfg.dashboard.port}"
            )
            print("Stop it first or use POST /api/archive/run (Phase 4).")
            sys.exit(2)
        embedder_inst = Embedder(cfg)
        embedder_inst.load()
        vs = VectorStore(cfg, embedder_inst)
        await vs.initialize()
        ms = MetadataStore(cfg)
        await ms.initialize()
        archiver = Archiver(cfg, vs, ms)
        wings = [args.wing] if args.wing else None
        report = await archiver.archive_all(dry_run=not args.apply, wings=wings)

        mode = "DRY-RUN" if report.dry_run else "APPLIED"
        print(f"=== Archive sweep [{mode}] ===")
        print(f"started:  {report.started_at}")
        print(f"finished: {report.finished_at}")
        if not report.dry_run:
            print(f"duplicates reconciled: {report.duplicates_reconciled}")
        print()
        for wr in report.wings:
            tag = f"SKIP ({wr.skipped_reason})" if wr.skipped else "OK"
            print(f"[{wr.wing}] {tag}")
            print(f"  candidates:        {wr.candidates}")
            print(f"  archived entries:  {wr.archived_entries}")
            print(f"  partitions:        {wr.partitions_written}")
            print(f"  bytes written:     {wr.archived_bytes}")
            print()
        return

    if sub_cmd == "stats":
        embedder_inst = Embedder(cfg)
        embedder_inst.load()
        vs = VectorStore(cfg, embedder_inst)
        await vs.initialize()
        ms = MetadataStore(cfg)
        await ms.initialize()
        archiver = Archiver(cfg, vs, ms)
        st = archiver.stats()
        print(json.dumps(st, indent=2))
        return

    if sub_cmd == "sql":
        embedder_inst = Embedder(cfg)
        embedder_inst.load()
        vs = VectorStore(cfg, embedder_inst)
        await vs.initialize()
        ms = MetadataStore(cfg)
        await ms.initialize()
        archiver = Archiver(cfg, vs, ms)
        try:
            rows = archiver.query_sql(sql_where=args.where, limit=args.limit)
        except RuntimeError as e:
            print(str(e))
            sys.exit(1)
        if not rows:
            print("No rows.")
            return
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, default=str))
        return

    if sub_cmd == "search":
        embedder_inst = Embedder(cfg)
        embedder_inst.load()
        vs = VectorStore(cfg, embedder_inst)
        await vs.initialize()
        ms = MetadataStore(cfg)
        await ms.initialize()
        archiver = Archiver(cfg, vs, ms)
        results = archiver.search_semantic(
            args.query, embedder_inst,
            wing=args.wing, since=args.since, limit=args.limit,
        )
        if not results:
            print("No matches.")
            return
        for r in results:
            print(f"  ({r.get('score', 0):.3f}) {r.get('id')}  {r.get('wing')}/{r.get('room')}")
            content = (r.get("content") or "")[:200]
            print(f"    {content}")
        return

    if sub_cmd == "restore":
        embedder_inst = Embedder(cfg)
        embedder_inst.load()
        vs = VectorStore(cfg, embedder_inst)
        await vs.initialize()
        ms = MetadataStore(cfg)
        await ms.initialize()
        archiver = Archiver(cfg, vs, ms)
        ok = await archiver.restore(args.entry_id)
        if not ok:
            print(f"Entry not found in archive: {args.entry_id}")
            sys.exit(1)
        print(f"Restored: {args.entry_id}")
        return

    print(f"Unknown archive subcommand: {sub_cmd}")
    sys.exit(1)


async def cmd_migrate_embeddings(args: argparse.Namespace) -> None:
    """Re-embed every entry with a different model. Offline only."""
    from .embedding_migrator import EmbeddingMigrator

    cfg = load_config(args.config)
    migrator = EmbeddingMigrator(cfg, args.to)
    report = await migrator.migrate(dry_run=not args.apply, batch_size=args.batch_size)

    mode = "DRY-RUN" if report.dry_run else "APPLIED"
    print(f"=== Embedding migration [{mode}] ===")
    print(f"started:  {report.started_at}")
    print(f"finished: {report.finished_at}")
    if report.error:
        print(f"ERROR: {report.error}")
        sys.exit(1)
    print(f"source: {report.source_model} ({report.source_dim}d)")
    print(f"target: {report.target_model} ({report.target_dim}d)")
    if report.backup_path:
        print(f"backup: {report.backup_path}")
    print(f"total entries:   {report.progress.total}")
    if not report.dry_run:
        print(f"embedded:        {report.progress.embedded}")
        print(f"uploaded:        {report.progress.uploaded}")
        print(f"skipped (empty): {report.progress.skipped}")
        if report.progress.failed_ids:
            print(f"failed ({len(report.progress.failed_ids)}): "
                  f"{', '.join(report.progress.failed_ids[:5])}"
                  + (" …" if len(report.progress.failed_ids) > 5 else ""))
        print()
        print(f"Update localmem.yaml: embedding.model: \"{report.target_model}\"")


def main():
    parser = argparse.ArgumentParser(
        prog="localmem",
        description="localmem — local-first multi-agent memory MCP server",
    )
    parser.add_argument(
        "-c", "--config",
        default="localmem.yaml",
        help="Path to config file (default: localmem.yaml)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # init
    p_init = sub.add_parser(
        "init",
        help="Scaffold a working localmem.yaml + data dir (no existing config needed)",
    )
    p_init.add_argument(
        "--data-dir", default=".",
        help="Directory to write localmem.yaml + data/ into (default: current dir)",
    )
    p_init.add_argument(
        "--wing", action="append",
        help="Wing name; pass multiple times for multiple wings (default: assistant)",
    )
    p_init.add_argument(
        "--dashboard", action="store_true",
        help="Enable the read-only dashboard sidecar in the generated config",
    )
    p_init.add_argument(
        "--qdrant-mode", choices=("local", "server"), default="local",
        help="local (embedded, single-writer) or server (URL-backed, multi-writer)",
    )
    p_init.add_argument(
        "--qdrant-url", default="http://localhost:6333",
        help="Qdrant URL when --qdrant-mode=server (default: http://localhost:6333)",
    )
    p_init.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing localmem.yaml",
    )

    # serve
    sub.add_parser("serve", help="Start the MCP server")

    # stats
    sub.add_parser("stats", help="Show storage statistics")

    # rooms
    p_rooms = sub.add_parser("rooms", help="List registered rooms")
    p_rooms.add_argument("--wing", help="Filter by wing")

    # triples
    p_triples = sub.add_parser("triples", help="Query triples")
    p_triples.add_argument("--subject", "-s", help="Filter by subject")
    p_triples.add_argument("--predicate", "-p", help="Filter by predicate")
    p_triples.add_argument("--all", "-a", action="store_true", help="Include inactive triples")

    # timeline
    p_timeline = sub.add_parser("timeline", help="Show predicate history")
    p_timeline.add_argument("subject", help="Subject to query")
    p_timeline.add_argument("predicate", help="Predicate to query")

    # diary
    p_diary = sub.add_parser("diary", help="Read diary entries")
    p_diary.add_argument("--agent", help="Filter by agent ID")
    p_diary.add_argument("--limit", type=int, default=20, help="Max entries (default: 20)")

    # graph
    p_graph = sub.add_parser("graph", help="Graph stats and patterns")
    p_graph.add_argument("--patterns", action="store_true", help="Show detected patterns")
    p_graph.add_argument("--min-freq", type=int, default=2, help="Min pattern frequency (default: 2)")

    # health
    sub.add_parser("health", help="Check store connectivity")

    # smoke
    sub.add_parser("smoke", help="Run full pipeline smoke test")

    # dashboard
    sub.add_parser("dashboard", help="Start dashboard REST+WS server (localhost:8782)")

    # intelligence
    p_intel = sub.add_parser("intelligence", help="Intelligence operations")
    p_intel.add_argument("intel_cmd", nargs="?", default="report",
                         choices=["detect", "alerts", "report"],
                         help="Sub-command (default: report)")
    p_intel.add_argument("--wing", help="Filter alerts by wing")
    p_intel.add_argument("--limit", type=int, default=10, help="Max alerts (default: 10)")

    # pin / unpin / list-pinned
    p_pin = sub.add_parser("pin", help="Pin an entry — exempt from retention")
    p_pin.add_argument("entry_id", help="Entry ID (UUID)")
    p_unpin = sub.add_parser("unpin", help="Unpin an entry")
    p_unpin.add_argument("entry_id", help="Entry ID (UUID)")
    p_pinned = sub.add_parser("list-pinned", help="List currently pinned entries")
    p_pinned.add_argument("--wing", help="Filter by wing")
    p_pinned.add_argument("--limit", type=int, default=100, help="Max rows (default: 100)")

    # prune
    p_prune = sub.add_parser(
        "prune",
        help="Retention sweep — consolidate stale low-importance entries (dry-run by default)",
    )
    p_prune.add_argument("--apply", action="store_true",
                         help="Actually run consolidation (default: dry-run)")
    p_prune.add_argument("--wing", help="Restrict to one wing")

    # archive
    p_archive = sub.add_parser("archive", help="Archive (cold tier) operations")
    archive_sub = p_archive.add_subparsers(dest="archive_cmd", required=True)

    p_arch_write = archive_sub.add_parser("write", help="Move past-max-age entries to cold tier (dry-run by default)")
    p_arch_write.add_argument("--apply", action="store_true", help="Actually transition entries")
    p_arch_write.add_argument("--wing", help="Restrict to one wing")

    archive_sub.add_parser("stats", help="Show on-disk archive statistics")

    p_arch_sql = archive_sub.add_parser("sql", help="Run a DuckDB SELECT against the archive (analytics)")
    p_arch_sql.add_argument("--where", help="Optional WHERE clause (without 'WHERE' keyword)")
    p_arch_sql.add_argument("--limit", type=int, default=100, help="Row limit (default: 100)")

    p_arch_search = archive_sub.add_parser("search", help="Semantic search over archived entries")
    p_arch_search.add_argument("query", help="Search text")
    p_arch_search.add_argument("--wing", help="Restrict to one wing")
    p_arch_search.add_argument("--since", help="Only files since YYYY-MM (lexicographic)")
    p_arch_search.add_argument("--limit", type=int, default=20, help="Result limit (default: 20)")

    p_arch_restore = archive_sub.add_parser("restore", help="Restore a cold-tier entry to hot tier")
    p_arch_restore.add_argument("entry_id", help="Entry ID (UUID)")

    # migrate-embeddings
    p_mig = sub.add_parser(
        "migrate-embeddings",
        help="Re-embed all entries with a different model (offline only)",
    )
    p_mig.add_argument("--to", required=True,
                       help="Target embedding model (e.g. BAAI/bge-large-en-v1.5)")
    p_mig.add_argument("--apply", action="store_true",
                       help="Actually run migration (default: dry-run)")
    p_mig.add_argument("--batch-size", type=int, default=500,
                       help="Embedding batch size (default: 500)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "init": cmd_init,
        "serve": cmd_serve,
        "stats": cmd_stats,
        "rooms": cmd_rooms,
        "triples": cmd_triples,
        "timeline": cmd_timeline,
        "diary": cmd_diary,
        "graph": cmd_graph,
        "health": cmd_health,
        "smoke": cmd_smoke,
        "dashboard": cmd_dashboard,
        "intelligence": cmd_intelligence,
        "pin": cmd_pin,
        "unpin": cmd_unpin,
        "list-pinned": cmd_list_pinned,
        "prune": cmd_prune,
        "archive": cmd_archive,
        "migrate-embeddings": cmd_migrate_embeddings,
    }

    # `init` is the bootstrap command — it CREATES the config that the other
    # commands read. Skip the config-load + logging-setup path for it.
    if args.command != "init":
        cfg = load_config(args.config)
        level_override = "DEBUG" if args.verbose else "WARNING"
        setup_logging(cfg, level_override=level_override)

    asyncio.run(commands[args.command](args))


if __name__ == "__main__":
    main()
