"""Detached job worker: ``python -m ldl_mcp.worker <job_id>``."""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Any

from . import auth, jobs
from .client import LDLClient
from .downloader import LoginExpiredError, SpeciesTarget, monograph_url_for, resolve_source_url, run_genus_job, safe_error_message
from .research_scope import load_scope
from .ldl_http import JobPaused, RequestsAPIContext
from .paths import now_iso


def find_genus_comb_obj_id(genus: str) -> int | None:
    rows = LDLClient().search_taxa(genus=genus, valid_only=True, limit=1000)
    for row in rows:
        if str(row.get("PresCombGenu") or "").casefold() == genus.casefold() and row.get("CombTaxStatus") == "valid":
            return int(row["CombObjID"])
    return int(rows[0]["CombObjID"]) if rows else None


def run(job_id: str) -> int:
    job = jobs.load(job_id)
    params: dict[str, Any] = job.get("params", {})
    jobs.update(job_id, status="running", pid=os.getpid(), started_at=now_iso(), runs=int(job.get("runs", 0)) + 1, message="")
    print(f"[{now_iso()}] worker {os.getpid()} started job {job_id} ({job.get('kind')})", flush=True)
    try:
        if job.get("kind") == "metadata":
            from .metadata import harvest

            summary = harvest(LDLClient(), params["scope_id"], should_stop=lambda: jobs.control_action(job_id), log=lambda m: print(m, flush=True))
            jobs.update(job_id, status="completed", finished_at=now_iso(), result=summary, message="Metadata harvested.")
            print(f"[{now_iso()}] finished: {summary['taxa_harvested']} taxa, {summary['documents_with_file']} documents", flush=True)
            return 0
        session = auth.build_session(require_cookies=bool(params.get("require_login", True)))
        request = RequestsAPIContext(session, control=lambda: jobs.control_action(job_id))
        targets = None
        if params.get("scope_id"):
            scope = load_scope(params["scope_id"])
            targets = [
                SpeciesTarget(t["accepted_name"], str(t["accepted_name"]).split(" ", 1)[-1], monograph_url_for(int(t["CombObjID"])))
                for t in scope["taxa"]
                if t.get("CombObjID")
            ]
            source_url = f"scope:{params['scope_id']}"
        else:
            source_url = job.get("source_url") or resolve_source_url(
                genus=params.get("genus"),
                comb_obj_id=params.get("comb_obj_id"),
                monograph_url=params.get("monograph_url"),
                find_comb_obj_id=find_genus_comb_obj_id,
            )
        jobs.update(job_id, source_url=source_url)

        def on_root(root: Path, genus: str) -> None:
            jobs.update(job_id, root=str(root), genus=genus)

        summary = run_genus_job(
            request,
            source_url,
            Path(params["output_parent"]).expanduser(),
            download_mode=params.get("download_mode", "fast"),
            refresh=bool(params.get("refresh")),
            verify_only=job.get("kind") == "verify",
            on_root=on_root,
            targets=targets,
            label=params.get("scope_label") or params.get("scope_id"),
        )
        needs_attention = bool(summary.get("failed_downloads")) or summary.get("passed") is False
        jobs.update(
            job_id,
            status="attention_required" if needs_attention else "completed",
            finished_at=now_iso(),
            result=summary,
            requests_made=request.request_count,
            message="Some documents still need attention; see 下载失败待补.csv." if needs_attention else "Done.",
        )
        print(f"[{now_iso()}] finished: {summary}", flush=True)
        return 0
    except JobPaused as exc:
        status = "cancelled" if exc.action == "cancel" else "paused"
        jobs.update(job_id, status=status, message=f"Stopped at a safe checkpoint ({exc.action}).")
        print(f"[{now_iso()}] {status}", flush=True)
        return 0
    except (LoginExpiredError, auth.NoSessionError) as exc:
        jobs.update(job_id, status="auth_required", message=f"{safe_error_message(exc)} Run auth_start, log in, then download_resume.")
        print(f"[{now_iso()}] auth_required: {safe_error_message(exc)}", flush=True)
        return 0
    except Exception as exc:
        jobs.update(job_id, status="failed", message=safe_error_message(exc))
        print(safe_error_message(traceback.format_exc()), flush=True)
        return 1


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m ldl_mcp.worker <job_id>", file=sys.stderr)
        return 2
    return run(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
