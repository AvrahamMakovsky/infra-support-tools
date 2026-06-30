#!/usr/bin/env python3
"""
spreadsheet_work_item_flow.py

Purpose:
    Turn spreadsheet rows into reviewed, human-approved work-item proposals.

Created by:
    Avraham Makovsky

License:
    MIT

Why it exists:
    Some operational workflows start as spreadsheets. This tool helps review
    rows, detect likely fields, prevent duplicate processing, and create
    mocked work-item references without connecting to a real ticketing system.

Public version:
    This version intentionally uses MockWorkItemService. It does not connect to
    any real company system, ticketing platform, or API.

Dependencies:
    Python standard library only.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional


APP_TITLE = "Spreadsheet Work Item Flow"
DB_FILE = "spreadsheet_work_item_flow.db"

COMPLETED_STATUSES = {"complete", "completed", "done", "finished", "closed", "resolved"}

FIELD_PATTERNS = {
    "status": ["status", "state", "progress", "stage"],
    "owner": ["owner", "assignee", "assigned", "responsible", "contact"],
    "host": ["host", "hostname", "machine", "system", "station", "device", "asset", "server"],
    "description": ["description", "details", "summary", "title", "issue", "problem", "task"],
    "completed_date": ["completed", "completion", "done date", "closed date", "date"],
    "priority": ["priority", "severity", "urgency", "impact"],
    "work_item_ref": ["ticket", "reference", "ref", "work item", "tracking", "id"],
}


@dataclass
class ColumnMapping:
    status: Optional[str] = None
    owner: Optional[str] = None
    host: Optional[str] = None
    description: Optional[str] = None
    completed_date: Optional[str] = None
    priority: Optional[str] = None
    work_item_ref: Optional[str] = None


@dataclass
class Proposal:
    row_number: int
    source: Dict[str, str]
    status: str = ""
    owner: str = ""
    host: str = ""
    description: str = ""
    completed_date: str = ""
    priority: str = ""
    existing_ref: str = ""
    recommendation: str = "skip"
    reason: str = ""
    proposal_hash: str = ""
    created_ref: str = ""


class DuplicateStore:
    """Small local SQLite store for duplicate protection between runs."""

    def __init__(self, db_path: str = DB_FILE) -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_items (
                    proposal_hash TEXT PRIMARY KEY,
                    created_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    host TEXT,
                    description_preview TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def has_seen(self, proposal_hash: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM processed_items WHERE proposal_hash = ?",
                (proposal_hash,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def save(self, proposal: Proposal) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO processed_items
                (proposal_hash, created_ref, created_at, host, description_preview)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    proposal.proposal_hash,
                    proposal.created_ref,
                    datetime.now().isoformat(timespec="seconds"),
                    proposal.host,
                    proposal.description[:160],
                ),
            )
            conn.commit()
        finally:
            conn.close()


class MockWorkItemService:
    """Public-safe placeholder for a real ticket/work-item integration."""

    def create(self, proposal: Proposal) -> str:
        digest = proposal.proposal_hash[:8].upper()
        return f"MOCK-{digest}"


def normalize(value: object) -> str:
    return "" if value is None else str(value).strip()


def detect_mapping(headers: List[str]) -> ColumnMapping:
    """Best-effort mapping from spreadsheet headers to logical fields."""
    mapping = ColumnMapping()
    lower_headers = {header: header.lower().strip() for header in headers}

    for field_name, patterns in FIELD_PATTERNS.items():
        for header, header_lower in lower_headers.items():
            if any(pattern in header_lower for pattern in patterns):
                setattr(mapping, field_name, header)
                break

    return mapping


def read_csv(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def build_hash(proposal: Proposal) -> str:
    """Create a stable hash from the fields that identify the work item."""
    material = json.dumps(
        {
            "host": proposal.host.lower(),
            "description": proposal.description.lower(),
            "completed_date": proposal.completed_date.lower(),
            "owner": proposal.owner.lower(),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_proposals(rows: List[Dict[str, str]], mapping: ColumnMapping, store: DuplicateStore) -> List[Proposal]:
    proposals = []

    for index, row in enumerate(rows, start=2):
        proposal = Proposal(row_number=index, source=row)
        proposal.status = normalize(row.get(mapping.status or ""))
        proposal.owner = normalize(row.get(mapping.owner or ""))
        proposal.host = normalize(row.get(mapping.host or ""))
        proposal.description = normalize(row.get(mapping.description or ""))
        proposal.completed_date = normalize(row.get(mapping.completed_date or ""))
        proposal.priority = normalize(row.get(mapping.priority or ""))
        proposal.existing_ref = normalize(row.get(mapping.work_item_ref or ""))

        proposal.proposal_hash = build_hash(proposal)
        status_lower = proposal.status.lower()

        if proposal.existing_ref:
            proposal.recommendation = "skip"
            proposal.reason = "Already has reference"
        elif store.has_seen(proposal.proposal_hash):
            proposal.recommendation = "skip"
            proposal.reason = "Already processed locally"
        elif status_lower and status_lower not in COMPLETED_STATUSES:
            proposal.recommendation = "review"
            proposal.reason = "Status is not completed"
        elif not proposal.description:
            proposal.recommendation = "skip"
            proposal.reason = "Missing description"
        else:
            proposal.recommendation = "create"
            proposal.reason = "Ready for review"

        proposals.append(proposal)

    return proposals


class App(tk.Tk):
    """Minimal Tkinter UI for reviewing and approving proposed rows."""

    def __init__(self) -> None:
        super().__init__()

        self.title(APP_TITLE)
        self.geometry("1180x720")

        self.store = DuplicateStore()
        self.service = MockWorkItemService()
        self.rows: List[Dict[str, str]] = []
        self.mapping = ColumnMapping()
        self.proposals: List[Proposal] = []

        self._build_ui()

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)

        ttk.Button(top, text="Open CSV", command=self.open_csv).pack(side=tk.LEFT)
        ttk.Button(top, text="Build proposals", command=self.rebuild_proposals).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Dry run", command=self.dry_run).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Create selected mock items", command=self.create_selected).pack(side=tk.LEFT, padx=6)

        self.status_var = tk.StringVar(value="Open a CSV file to begin.")
        ttk.Label(top, textvariable=self.status_var).pack(side=tk.LEFT, padx=16)

        columns = (
            "selected",
            "row",
            "recommendation",
            "reason",
            "host",
            "owner",
            "status",
            "priority",
            "description",
            "created_ref",
        )

        self.tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="extended")
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        widths = {
            "selected": 80,
            "row": 60,
            "recommendation": 120,
            "reason": 180,
            "host": 140,
            "owner": 120,
            "status": 120,
            "priority": 90,
            "description": 300,
            "created_ref": 130,
        }

        for column in columns:
            self.tree.heading(column, text=column)
            self.tree.column(column, width=widths.get(column, 120), anchor=tk.W)

        self.tree.bind("<Double-1>", self.toggle_selected)

        ttk.Label(
            self,
            text="Double-click a row to toggle selection. Public version uses mocked work-item creation only.",
            padding=(10, 0, 10, 10),
        ).pack(fill=tk.X)

    def open_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="Open spreadsheet CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )

        if not path:
            return

        try:
            self.rows = read_csv(path)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not read CSV:\n{exc}")
            return

        headers = list(self.rows[0].keys()) if self.rows else []
        self.mapping = detect_mapping(headers)
        self.status_var.set(f"Loaded {len(self.rows)} rows from {Path(path).name}.")
        self.rebuild_proposals()

    def rebuild_proposals(self) -> None:
        if not self.rows:
            messagebox.showinfo(APP_TITLE, "Open a CSV file first.")
            return

        self.proposals = build_proposals(self.rows, self.mapping, self.store)
        self.refresh_tree()

    def refresh_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        for index, proposal in enumerate(self.proposals):
            selected = "yes" if proposal.recommendation == "create" else "no"
            self.tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    selected,
                    proposal.row_number,
                    proposal.recommendation,
                    proposal.reason,
                    proposal.host,
                    proposal.owner,
                    proposal.status,
                    proposal.priority,
                    proposal.description[:120],
                    proposal.created_ref,
                ),
            )

        self.status_var.set(f"Built {len(self.proposals)} proposals.")

    def toggle_selected(self, event: object = None) -> None:
        focused = self.tree.focus()
        if not focused:
            return

        values = list(self.tree.item(focused, "values"))
        values[0] = "no" if values[0] == "yes" else "yes"
        self.tree.item(focused, values=values)

    def selected_indices(self) -> List[int]:
        selected = []

        for item in self.tree.get_children():
            values = self.tree.item(item, "values")
            if values and values[0] == "yes":
                selected.append(int(item))

        return selected

    def dry_run(self) -> None:
        indices = self.selected_indices()
        messagebox.showinfo(APP_TITLE, f"Dry run: {len(indices)} rows selected for mocked creation.")

    def create_selected(self) -> None:
        indices = self.selected_indices()

        if not indices:
            messagebox.showinfo(APP_TITLE, "No rows selected.")
            return

        if not messagebox.askyesno(APP_TITLE, f"Create {len(indices)} mocked work items?"):
            return

        for index in indices:
            proposal = self.proposals[index]
            proposal.created_ref = self.service.create(proposal)
            self.store.save(proposal)

        self.refresh_tree()
        messagebox.showinfo(APP_TITLE, "Mock creation completed.")


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
