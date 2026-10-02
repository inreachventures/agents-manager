"""Dashboard (Textual). Runs in the `home` session of the manager tmux server."""

from __future__ import annotations

import time

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import DataTable, Footer, Input, Label, LoadingIndicator, Static

from . import db, fetcher, gh, tmux, view, workstream
from .config import get_config
from .repos import discover

ICON = {db.NEEDS_YOU: "⚠", db.WORKING: "●", db.YOUR_TURN: "◐", db.STOPPED: "○", db.ARCHIVED: "·"}
STYLE = {db.NEEDS_YOU: "bold dark_orange", db.WORKING: "green", db.YOUR_TURN: "cyan", db.STOPPED: "grey50"}
PR_REFRESH_SECONDS = 60


def ago(ts: float) -> str:
    s = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


class Prompt(ModalScreen[str | None]):
    """Single-line input with optional autocomplete."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str = "", suggestions: list[str] | None = None):
        super().__init__()
        self.title_text, self.placeholder, self.suggestions = title, placeholder, suggestions

    def compose(self) -> ComposeResult:
        suggester = SuggestFromList(self.suggestions, case_sensitive=False) if self.suggestions else None
        with Vertical(id="dialog"):
            yield Label(self.title_text)
            yield Input(placeholder=self.placeholder, suggester=suggester)
            yield Label("enter: ok · →: accept suggestion · esc: cancel", classes="hint")

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class Busy(ModalScreen[None]):
    """Spinner shown while a slow action (git fetch, gh, worktree changes) runs in a worker thread."""

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message)
            yield LoadingIndicator()


class Confirm(ModalScreen[str | None]):
    BINDINGS = [Binding("y", "choose('yes')", "Yes"), Binding("f", "choose('force')", "Force"),
                Binding("escape,n", "choose(None)", "Cancel")]

    def __init__(self, text: str, allowed: bool, verb: str = "archive", can_force: bool = True):
        super().__init__()
        self.text, self.allowed, self.verb, self.can_force = text, allowed, verb, can_force

    def compose(self) -> ComposeResult:
        hint = f"y: {self.verb} · esc: cancel" if self.allowed else "blocked — f: force (discards work) · esc: cancel"
        with Vertical(id="dialog"):
            yield Static(self.text)
            yield Label(hint, classes="hint")

    def action_choose(self, choice: str | None) -> None:
        if (choice == "yes" and not self.allowed) or (choice == "force" and not self.can_force):
            return
        self.dismiss(choice)


class Dashboard(App):
    TITLE = "Workstreams"
    CSS = """
    DataTable { height: 1fr; }
    #dialog { width: 90; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    Prompt, Confirm, Busy { align: center middle; }
    Busy LoadingIndicator { height: 3; }
    .hint { color: $text-muted; margin-top: 1; }
    """
    BINDINGS = [
        Binding("q", "detach", "Exit"),  # first, so narrow terminals don't cut it off
        Binding("enter", "open", "Open"),
        Binding("n", "new", "New"),
        Binding("a", "add_repo", "Add repo"),
        Binding("r", "resume", "Resume"),
        Binding("b", "rebase", "Rebase"),
        Binding("x", "archive", "Archive"),
        Binding("u", "unarchive", "Restore"),
        Binding("p", "refresh_prs", "Refresh"),
        Binding("v", "toggle_archived", "Archived"),
    ]
    ACTIVE_ONLY = {"open", "new", "add_repo", "resume", "rebase", "archive", "refresh_prs"}

    def __init__(self):
        super().__init__()
        self.rows: list[view.Row] = []
        self.last_pr_refresh = 0.0
        self.show_archived = False

    def compose(self) -> ComposeResult:
        yield Static("", id="summary")
        yield DataTable(cursor_type="row", zebra_stripes=False)
        yield Footer(compact=True)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("", "Workstream", "Status", "Age", "Repo", "Branch", "Git", "PR")
        self.reload()
        self.fetch_bases()
        self.set_interval(2, self.reload)
        self.set_interval(30, self.fetch_bases)  # each repo is actually fetched at most every 5 minutes

    # ------------------------------------------------------------------ data

    @work(thread=True, exclusive=True, group="reload")
    def reload(self) -> None:
        if time.time() - self.last_pr_refresh > PR_REFRESH_SECONDS:
            self.last_pr_refresh = time.time()
            self.refresh_prs()
        archived = self.show_archived
        rows = view.load(include_archived=archived, git_status=not archived)
        rows = [r for r in rows if (r.status == db.ARCHIVED) == archived]
        self.call_from_thread(self.render_rows, rows, archived)

    @work(thread=True, exclusive=True, group="fetch")
    def fetch_bases(self, max_age: float = fetcher.DASHBOARD_MAX_AGE) -> None:
        errors = fetcher.fetch_stale(max_age)
        failed = [p for p, e in errors.items() if e]
        if failed:
            self.call_from_thread(self.notify, f"could not fetch: {', '.join(failed)}", severity="warning")

    def refresh_prs(self) -> None:
        conn = db.connect()
        for ws in db.list_workstreams(conn):
            gh.refresh(conn, ws.key)

    def render_rows(self, rows: list[view.Row], archived: bool = False) -> None:
        if archived != self.show_archived:
            return  # loaded before the view was toggled
        table = self.query_one(DataTable)
        selected = self.selected_ws()
        self.rows = rows
        table.clear()
        for r in rows:
            style = STYLE.get(r.status, "")
            status = f"[{style}]{r.status_text}[/]" if style else r.status_text
            if r.all_merged:
                status += {
                    "pass": " [bold green]· merged, CI green → x archive[/]",
                    "fail": " [bold red]· merged, CI failing on base[/]",
                    "pending": " [yellow]· merged, CI running on base…[/]",
                    "n/a": " [yellow]· merged, CI unknown[/]",
                }.get(r.base_ci, " [magenta]· merged (no CI) → x archive[/]")
            # One (multi-line) row per workstream, one line per repo, so the cursor moves workstream by workstream.
            repo_lines = [self._repo_cells(rr) for rr in r.repos] or [self._repo_cells(None)]
            age = ago(r.ws.archived_at or r.ws.status_at)
            table.add_row(
                ICON.get(r.status, "?"), f"[b]{r.ws.label}[/b]", status, age,
                *("\n".join(col) for col in zip(*repo_lines, strict=True)), key=r.ws.key, height=len(repo_lines),
            )
        if archived:
            n = len(rows)
            self.query_one("#summary", Static).update(
                f" [b]Archived[/b] · {n} workstream{'s' * (n != 1)}   [grey50]u: restore · v: back to active[/]")
            self._restore_cursor(table, selected)
            return
        oks = [rr.fetched_ok_at for r in rows for rr in r.repos]
        need = sum(r.status == db.NEEDS_YOU for r in rows)
        turn = sum(r.status == db.YOUR_TURN for r in rows)
        summary = f" {len(rows)} workstreams"
        if need:
            summary += f"   [bold dark_orange]⚠ {need} need you[/]"
        if turn:
            summary += f"   ◐ {turn} your turn"
        if oks:
            oldest = None if None in oks else min(oks)
            summary += "   [grey50]origin checked " + (f"{ago(oldest)} ago" if oldest else "…") + "[/]"
        self.query_one("#summary", Static).update(summary)
        self._restore_cursor(table, selected)

    @staticmethod
    def _restore_cursor(table: DataTable, selected: str | None) -> None:
        if selected:
            for idx, r in enumerate(table.rows):
                if r.value == selected:
                    table.move_cursor(row=idx)
                    break

    @staticmethod
    def _repo_cells(rr: view.RepoRow | None) -> tuple[str, str, str, str]:
        if rr is None:
            return ("[grey50]no repos yet[/]", "", "", "")
        git = f"[{rr.style}]{escape(rr.state)}[/]" if rr.style else escape(rr.state)
        if rr.behind:
            git += f" [{'yellow' if rr.behind >= fetcher.BEHIND_WARN else 'grey50'}]· {rr.behind} behind base[/]"
        if rr.fetch_error:
            git += " [red]· fetch failed[/]"
        return (rr.link.repo, rr.link.branch, git, rr.pr)

    def selected_ws(self) -> str | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:  # noqa: BLE001
            return None
        return key

    # ------------------------------------------------------------------ actions

    def check_action(self, action: str, parameters) -> bool | None:
        # the archived view only offers restore; the active view hides restore
        if action in self.ACTIVE_ONLY:
            return not self.show_archived
        if action == "unarchive":
            return self.show_archived
        return True

    def action_toggle_archived(self) -> None:
        self.set_view(archived=not self.show_archived)

    def set_view(self, archived: bool) -> None:
        self.show_archived = archived
        self.query_one(DataTable).clear()
        self.refresh_bindings()
        self.reload()

    def action_unarchive(self) -> None:
        key = self.selected_ws()
        if not key:
            return

        def done(log: list[str]) -> None:
            self.notify("\n".join(log), timeout=8)
            self.set_view(archived=False)  # back to the active view, where it can be opened

        self.run_busy(f"Restoring {key}: fetching and recreating worktrees…",
                      lambda: workstream.unarchive(key), done)

    def run_busy(self, message: str, fn, on_done) -> None:
        """Run `fn` in a thread behind a spinner, then call `on_done(result)` on the UI thread."""
        busy = Busy(message)
        self.push_screen(busy)

        def finish(result, error) -> None:
            if self.screen is busy:
                self.pop_screen()
            if error is not None:
                self.notify(str(error), severity="error", timeout=8)
            else:
                on_done(result)

        def job() -> None:
            try:
                result, error = fn(), None
            except Exception as e:  # noqa: BLE001
                result, error = None, e
            self.call_from_thread(finish, result, error)

        self.run_worker(job, thread=True, group="busy")

    def _guard(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            self.notify(str(e), severity="error", timeout=8)
            return None

    @on(DataTable.RowSelected)
    def row_selected(self, _: DataTable.RowSelected) -> None:
        if self.show_archived:
            self.notify("archived — press u to restore it first")
        else:
            self.action_open()

    def action_open(self) -> None:
        if key := self.selected_ws():
            self._guard(workstream.open_, key)

    def action_resume(self) -> None:
        if key := self.selected_ws():
            if self._guard(workstream.resume, key):
                self.notify(f"{key} resumed")
                self.reload()

    def action_new(self) -> None:
        def done(ticket: str | None) -> None:
            if ticket is None:
                return
            ws = self._guard(workstream.new, ticket=ticket or None)
            if ws:
                self._guard(workstream.open_, ws.key)

        self.push_screen(Prompt("New workstream — ticket id (optional, enter to skip; describe the task in Claude)",
                                "PROJ-313"), done)

    def action_add_repo(self) -> None:
        key = self.selected_ws()
        if not key:
            return
        names = [r.name for r in discover(get_config())]

        def done(repo: str | None) -> None:
            if repo:
                self.run_busy(
                    f"Attaching {repo} to {key}: fetching and creating the worktree…",
                    lambda: workstream.add_repo(key, repo),
                    lambda res: (self.notify(f"attached {res.link.repo} on {res.link.branch}"), self.reload()),
                )

        self.push_screen(Prompt(f"Add repo to {key}", "repo name", names), done)

    def action_archive(self) -> None:
        key = self.selected_ws()
        if not key:
            return

        def confirm(result) -> None:
            ws, reports = result
            lines = [f"[b]Archive {ws.label}?[/b]", ""]
            for r in reports:
                state, style = view.git_state(r.link, r.status, r.pr, after_pr=r.after_pr)
                lines.append(f"{r.link.repo}  {escape(r.link.branch)}  [{style}]{escape(state)}[/]  "
                             f"{gh.describe(r.pr)}")
                lines += [f"   [red]✗ {b}[/]" for b in r.blockers]
            blocked = any(r.blockers for r in reports)
            self.push_screen(Confirm("\n".join(lines), not blocked), archive)

        def archive(choice: str | None) -> None:
            if not choice:
                return
            self.run_busy(
                f"Archiving {key}: stopping the session, removing worktrees…",
                lambda: workstream.archive(key, force=choice == "force"),
                lambda log: (self.notify("\n".join(log), timeout=8), self.reload()),
            )

        self.run_busy(f"Checking {key}: fetching repos and PR status…",
                      lambda: workstream.cleanup_report(key), confirm)

    def action_rebase(self) -> None:
        key = self.selected_ws()
        if not key:
            return

        def confirm(result) -> None:
            ws, steps = result
            if all(s.skip for s in steps):
                reasons = "; ".join(f"{s.link.repo}: {s.skip}" for s in steps) or "no repos attached"
                self.notify(f"nothing to rebase — {reasons}", timeout=8)
                return
            lines = [f"[b]Rebase {escape(ws.label)}?[/b]", ""]
            for s in steps:
                if s.skip:
                    lines.append(f"[grey50]{s.link.repo}  skipped: {escape(s.skip)}[/]")
                else:
                    push = "then force-push (with lease)" if s.pushed_at else "not on origin yet, so no push"
                    lines.append(f"{s.link.repo}  {escape(s.link.branch)}  {s.behind} behind {s.link.base}"
                                 f" → rebase, {push}")
            lines += ["", "[grey50]A rebase that conflicts is aborted and handed to Claude to resolve.[/]"]
            self.push_screen(Confirm("\n".join(lines), True, verb="rebase", can_force=False), rebase)

        def rebase(choice: str | None) -> None:
            if not choice:
                return

            def done(log: list[str]) -> None:
                self.notify("\n".join(log), timeout=10)
                self.last_pr_refresh = 0  # the PR's head moved: re-read its checks
                self.reload()

            self.run_busy(f"Rebasing {key}…", lambda: workstream.rebase(key), done)

        self.run_busy(f"Checking {key}: fetching origin…", lambda: workstream.rebase_plan(key), confirm)

    def action_refresh_prs(self) -> None:
        self.last_pr_refresh = 0
        self.fetch_bases(max_age=0)
        self.reload()

    def action_detach(self) -> None:
        tmux.run("detach-client", check=False)

    async def action_quit(self) -> None:
        # ctrl+q: quitting would end the `home` session and tmux would drop you into a workstream instead
        self.action_detach()


def run() -> None:
    Dashboard().run()
