"""Tab — Manual: paste/upload a screenshot or PDF of a retailer's rate page and
extract the plan straight into the comparison engine.

This is the fast path when you already have the rates on screen: instead of
letting the autonomous agent drive a browser, you simply

  1. optionally enter the **provider** (retailer) name and **plan name**
     (leave them blank to have the model infer them from the page),
  2. paste (Ctrl/Cmd-V) or browse for a **screenshot** of the rates — or upload
     a **PDF** rate sheet (Browse… / drag & drop),
  3. click **Extract Plan**.

If the provider name is empty on the first click, a lightweight identity-inference
call reads just the retailer and plan name off the rate page, pre-fills the
fields, and asks you to confirm; the full rate extraction runs on the second
click. Because the retailer (and optionally the plan name) are then supplied,
the LLM only has to read the rates — which works far more reliably with smaller
local models than asking them to also identify the brand from a screenshot.

Each extracted plan is upserted to ``data/plans/{plan_id}.json`` so it persists
across restarts and is picked up by the Analyse tab.

PDFs are rendered to an image (pages stitched together) and their text is
extracted, so both vision and text-only models can read them.

The LLM configuration is shared with the Agent tab.
"""

from __future__ import annotations

import io
import logging
import threading
from pathlib import Path
from tkinter import filedialog
from typing import Any, Callable, Optional

import customtkinter as ctk

from power_analyser.agent.extractors.plan_extractor import PlanExtractor
from power_analyser.core.tariff.loader import save_plan
from power_analyser.core.tariff.schema import ElectricityPlan

from ..widgets.llm_config_frame import LLMConfigFrame, default_llm_vars

logger = logging.getLogger(__name__)

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tiff")
_PDF_EXTS = (".pdf",)
_ACCEPTED_EXTS = _IMAGE_EXTS + _PDF_EXTS
_PREVIEW_MAX = 220  # max preview dimension in pixels — bounded so the action
                    # row (Extract button + result box) always stays visible,
                    # even when a large multi-page PDF is dropped in.


class ManualView(ctk.CTkFrame):
    """Tab — manual screenshot extraction of a single retailer rate page."""

    def __init__(
        self,
        parent,
        on_plans_found: Callable[[list[ElectricityPlan]], None],
        settings: Optional[dict[str, Any]] = None,
        llm_vars: Optional[dict[str, ctk.StringVar]] = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, **kwargs)
        self._on_plans_found = on_plans_found
        self._settings = settings or {}
        self._llm_vars = llm_vars if llm_vars is not None else default_llm_vars()
        self._image_bytes: Optional[bytes] = None
        self._pdf_text: Optional[str] = None  # text extracted from an uploaded PDF
        self._preview_image: Optional[ctk.CTkImage] = None  # keep ref alive
        self._running = False

        self._build_ui()
        self._apply_settings()
        self._setup_dnd()

        # Ctrl/Cmd-V pastes a screenshot from the clipboard (unless an entry
        # has focus, in which case the normal text paste wins). CustomTkinter
        # forbids ``bind_all``, so bind on the toplevel window; any failure is
        # swallowed so the app still starts (the Paste button is the fallback).
        self._bind_paste()

    def _bind_paste(self) -> None:
        try:
            toplevel = self.winfo_toplevel()
        except Exception:
            return
        handler = self._on_paste_key
        for seq in ("<Control-v>", "<Command-v>"):
            try:
                toplevel.bind(seq, handler, add="+")
            except Exception:
                pass

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        # Row 2 (the Rate page panel) is not weighted, so it stays at the
        # natural size of its (bounded) preview image. Row 3 (action: Extract
        # button + result box) gets all remaining space with a guaranteed
        # ``minsize`` so the button can never be pushed off-screen by a large
        # PDF preview. Tk's grid has no ``maxsize`` for rows, so the preview
        # image itself is capped via ``_PREVIEW_MAX`` in ``_show_preview``.
        self.rowconfigure(2, weight=0)
        self.rowconfigure(3, weight=1, minsize=200)

        # ── Shared LLM config ──
        self._llm_config = LLMConfigFrame(self, shared_vars=self._llm_vars)
        self._llm_config.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")

        # ── Plan identity (provider + optional plan name) ──
        id_frame = ctk.CTkFrame(self)
        id_frame.grid(row=1, column=0, padx=16, pady=8, sticky="ew")
        id_frame.columnconfigure((1, 3), weight=1)

        ctk.CTkLabel(id_frame, text="Provider name:").grid(row=0, column=0, padx=8, pady=6, sticky="w")
        self._retailer_entry = ctk.CTkEntry(id_frame, placeholder_text="e.g. Amber, Origin, Red Energy")
        self._retailer_entry.grid(row=0, column=1, padx=8, pady=6, sticky="ew")

        ctk.CTkLabel(id_frame, text="Plan name (optional):").grid(row=0, column=2, padx=8, pady=6, sticky="w")
        self._plan_name_entry = ctk.CTkEntry(id_frame, placeholder_text="e.g. Smart Plan")
        self._plan_name_entry.grid(row=0, column=3, padx=8, pady=6, sticky="ew")

        # ── Screenshot panel ──
        shot_frame = ctk.CTkFrame(self)
        shot_frame.grid(row=2, column=0, padx=16, pady=8, sticky="ew")
        shot_frame.columnconfigure(0, weight=1)

        top = ctk.CTkFrame(shot_frame, fg_color="transparent")
        top.grid(row=0, column=0, padx=8, pady=(8, 4), sticky="ew")
        ctk.CTkLabel(
            top, text="Rate page (screenshot or PDF)", font=ctk.CTkFont(size=13, weight="bold")
        ).pack(side="left")
        ctk.CTkButton(top, text="Paste (Ctrl+V)", width=110, command=self._paste_from_clipboard).pack(
            side="right", padx=(6, 0)
        )
        ctk.CTkButton(top, text="Browse…", width=90, command=self._browse_image).pack(side="right")

        self._preview_label = ctk.CTkLabel(
            shot_frame,
            text="Paste a screenshot (Ctrl+V) or click Browse…\n"
            "Screenshots and PDF rate sheets are both accepted.\n"
            "You can also drag & drop a file here.",
            justify="center",
            fg_color=("gray86", "gray20"),
            height=200,
        )
        self._preview_label.grid(row=1, column=0, padx=8, pady=(4, 8), sticky="nsew")

        # ── Action + status ──
        action = ctk.CTkFrame(self, fg_color="transparent")
        action.grid(row=3, column=0, padx=16, pady=(0, 16), sticky="ews")
        action.columnconfigure(0, weight=1)
        action.rowconfigure(1, weight=1)

        self._extract_btn = ctk.CTkButton(
            action, text="Extract Plan", height=34, command=self._start_extract
        )
        self._extract_btn.grid(row=0, column=0, pady=(0, 6), sticky="ew")

        self._result_box = ctk.CTkTextbox(action, height=120, state="disabled")
        self._result_box.grid(row=1, column=0, sticky="nsew")

    # ── Settings ───────────────────────────────────────────────────────────────

    def _apply_settings(self) -> None:
        self._llm_config.apply_settings(self._settings)

    def collect_state(self, settings: dict[str, Any]) -> None:
        self._llm_config.collect_state(settings)

    # ── Image acquisition ──────────────────────────────────────────────────────

    def _on_paste_key(self, _event) -> None:
        """Handle Ctrl/Cmd-V: paste an image unless a text field is focused."""
        focused = self.focus_get()
        if isinstance(focused, ctk.CTkEntry):
            return  # let the normal text paste happen
        self._paste_from_clipboard()

    def _paste_from_clipboard(self) -> None:
        data, err = _read_clipboard_image()
        if err:
            self._set_result(err)
            return
        self._set_image(data, source="clipboard")

    def _browse_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Select a screenshot or PDF of the rates",
            filetypes=[
                ("Images and PDFs", " ".join(f"*{e}" for e in _ACCEPTED_EXTS)),
                ("Image files", " ".join(f"*{e}" for e in _IMAGE_EXTS)),
                ("PDF files", "*.pdf"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self._load_image_file(Path(path))

    def _load_image_file(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._set_result(f"Could not read file: {exc}")
            return
        from power_analyser.agent.extractors.pdf_utils import is_pdf

        if path.suffix.lower() in _PDF_EXTS or is_pdf(data):
            self._load_pdf(data, source=str(path))
        else:
            self._set_image(data, source=str(path))

    def _load_pdf(self, data: bytes, source: str) -> None:
        """Render a PDF to a preview image and extract its text for the LLM.

        Rendering runs in a background thread so a large multi-page PDF does
        not freeze the Tk event loop. The result is marshalled back via
        ``self.after`` on the GUI thread.
        """
        self._set_result("Rendering PDF…")
        self._extract_btn.configure(state="disabled")
        thread = threading.Thread(
            target=self._run_pdf_load,
            args=(data, source),
            daemon=True,
        )
        thread.start()

    def _run_pdf_load(self, data: bytes, source: str) -> None:
        try:
            from power_analyser.agent.extractors.pdf_utils import (
                pdf_to_image_and_text,
            )

            png_bytes, text = pdf_to_image_and_text(data)
        except Exception as exc:
            self.after(0, lambda e=exc: self._on_pdf_load_error(e))
            return
        self.after(0, lambda: self._on_pdf_load_done(png_bytes, text, source))

    def _on_pdf_load_done(self, png_bytes: bytes, text: str, source: str) -> None:
        if not self._running:  # don't clobber an in-flight extraction's button state
            self._extract_btn.configure(state="normal")
        self._pdf_text = text or None
        self._image_bytes = png_bytes
        self._show_preview(png_bytes)
        page_note = " (no selectable text — using the rendered image)" if not text else ""
        self._set_result(f"PDF loaded from {source}{page_note}. Ready to extract.")

    def _on_pdf_load_error(self, exc: Exception) -> None:
        from power_analyser.agent.extractors.pdf_utils import friendly_pdf_error

        if not self._running:
            self._extract_btn.configure(state="normal")
        self._set_result(friendly_pdf_error(exc))

    def _set_image(self, data: bytes, source: str) -> None:
        """Adopt *data* as the current screenshot, normalised to PNG + preview."""
        normalised, err = _normalise_to_png(data)
        if err:
            self._set_result(err)
            return
        self._pdf_text = None  # image source, not a PDF
        self._image_bytes = normalised
        self._show_preview(normalised)
        self._set_result(f"Screenshot loaded from {source}. Ready to extract.")

    def _show_preview(self, png_bytes: bytes) -> None:
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(png_bytes))
        except Exception as exc:
            logger.debug("Could not build preview: %s", exc)
            return
        img.thumbnail((_PREVIEW_MAX, _PREVIEW_MAX))
        self._preview_image = ctk.CTkImage(
            light_image=img, dark_image=img, size=img.size
        )
        self._preview_label.configure(image=self._preview_image, text="")

    # ── Drag & drop (optional, via tkdnd) ──────────────────────────────────────

    def _setup_dnd(self) -> None:
        try:
            from tkinterdnd2 import DND_FILES
        except ImportError:
            return
        try:
            self._preview_label.drop_target_register(DND_FILES)
            self._preview_label.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event) -> None:
        from ..views.core_view import _parse_dropped_files  # reuse existing helper

        files = _parse_dropped_files(getattr(event, "data", ""))
        for f in files:
            if f.lower().endswith(_ACCEPTED_EXTS):
                self._load_image_file(Path(f))
                return

    # ── Extraction ─────────────────────────────────────────────────────────────

    def _start_extract(self) -> None:
        if self._running:
            return
        if not self._image_bytes:
            self._set_result("No file loaded. Paste a screenshot (Ctrl+V), click Browse…, or drop a PDF.")
            return

        retailer = self._retailer_entry.get().strip()
        plan_name = self._plan_name_entry.get().strip()

        try:
            config = self._llm_config.build_config()
        except Exception as exc:
            self._set_result(f"ERROR building config: {exc}")
            return

        from power_analyser.agent.llm.base import create_provider

        try:
            provider = create_provider(config)
        except Exception as exc:
            self._set_result(f"ERROR creating LLM provider: {exc}")
            return

        # Two-phase flow: if the provider (retailer) name is still empty, ask
        # the LLM to read it (plus the plan name) off the rate page, pre-fill
        # the fields, and ask the user to confirm. The full extraction runs on
        # the NEXT click, once the provider name is populated. This keeps a
        # small model honest: it only has to confirm identity, not guess it
        # silently while also reading the rates.
        if not retailer:
            self._infer_identity(provider)
            return

        self._begin_extraction(provider, retailer, plan_name)

    # ── Phase 1: infer retailer / plan name ────────────────────────────────────

    def _infer_identity(self, provider) -> None:
        self._extract_btn.configure(state="disabled", text="Identifying…")
        self._set_result("Reading the provider and plan name from the rate page…")
        image_bytes = self._image_bytes
        page_text = self._pdf_text or ""
        thread = threading.Thread(
            target=self._run_identity_inference,
            args=(provider, image_bytes, page_text),
            daemon=True,
        )
        thread.start()

    def _run_identity_inference(
        self, provider, image_bytes: bytes, page_text: str
    ) -> None:
        try:
            extractor = PlanExtractor(provider)
            retailer, plan_name = extractor.infer_identity_from_screenshot(
                image_bytes, page_text=page_text
            )
            self.after(
                0, lambda r=retailer, p=plan_name: self._on_identity_inferred(r, p)
            )
        except Exception as exc:
            self.after(0, lambda e=exc: self._on_identity_error(e))

    def _on_identity_inferred(self, retailer: str, plan_name: str) -> None:
        self._extract_btn.configure(state="normal", text="Extract Plan")
        if not retailer:
            self._set_result(
                "Couldn't determine the provider name from the rate page. "
                "Please enter the provider (retailer) name manually, then click "
                "Extract Plan."
            )
            return
        # Pre-fill the identity fields so the user can confirm or correct them.
        self._retailer_entry.delete(0, "end")
        self._retailer_entry.insert(0, retailer)
        if plan_name:
            self._plan_name_entry.delete(0, "end")
            self._plan_name_entry.insert(0, plan_name)
        msg = f"I've pre-populated the provider ({retailer!r})"
        if plan_name:
            msg += f" and plan name ({plan_name!r})"
        msg += (
            " from the rate page. Please check them and click Extract Plan "
            "again to continue."
        )
        self._set_result(msg)

    def _on_identity_error(self, exc: Exception) -> None:
        self._extract_btn.configure(state="normal", text="Extract Plan")
        self._set_result(f"ERROR inferring identity: {exc}")

    # ── Phase 2: full extraction ───────────────────────────────────────────────

    def _begin_extraction(self, provider, retailer: str, plan_name: str) -> None:
        self._running = True
        self._extract_btn.configure(state="disabled", text="Extracting…")
        self._set_result("Sending the rate page to the LLM…")
        image_bytes = self._image_bytes
        page_text = self._pdf_text or ""
        thread = threading.Thread(
            target=self._run_extraction,
            args=(provider, image_bytes, retailer, plan_name, page_text),
            daemon=True,
        )
        thread.start()

    def _run_extraction(
        self,
        provider,
        image_bytes: bytes,
        retailer: str,
        plan_name: str,
        page_text: str,
    ) -> None:
        try:
            extractor = PlanExtractor(provider)
            plans = extractor.extract_from_screenshot_with_context(
                image_bytes, retailer=retailer, plan_name=plan_name, page_text=page_text
            )
            self.after(0, lambda: self._on_extract_done(plans))
        except Exception as exc:
            self.after(0, lambda e=exc: self._on_extract_error(e))

    def _on_extract_done(self, plans: list[ElectricityPlan]) -> None:
        self._running = False
        self._extract_btn.configure(state="normal", text="Extract Plan")
        if not plans:
            self._set_result(
                "No plan could be extracted. Check that the model supports vision, "
                "or try a clearer screenshot / PDF of the rates."
            )
            return

        # Upsert each plan to data/plans/{plan_id}.json so the result persists
        # and is picked up by the Analyse tab / comparison engine. Failures are
        # non-fatal — the plans still flow into the in-memory comparison set.
        saved_paths: list[str] = []
        for p in plans:
            try:
                saved_paths.append(str(save_plan(p)))
            except Exception as exc:
                logger.warning("Could not save plan %s: %s", p.plan_id, exc)

        lines = [f"Extracted {len(plans)} plan(s):"]
        for p in plans:
            supply = p.daily_supply_charge
            rates = ", ".join(f"{t.name} {t.rate}$/kWh" for t in p.usage_tiers)
            lines.append(f"• {p.retailer} — {p.plan_name}  |  supply {supply}$/day  |  {rates}")
        if saved_paths:
            lines.append("")
            lines.append("Saved to:")
            lines.extend(f"  {p}" for p in saved_paths)
        self._set_result("\n".join(lines))
        self._on_plans_found(plans)

    def _on_extract_error(self, exc: Exception) -> None:
        self._running = False
        self._extract_btn.configure(state="normal", text="Extract Plan")
        self._set_result(f"ERROR: {exc}")

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def _set_result(self, text: str) -> None:
        self._result_box.configure(state="normal")
        self._result_box.delete("1.0", "end")
        self._result_box.insert("end", text + "\n")
        self._result_box.configure(state="disabled")


# ── Module-level image helpers (kept import-light; PIL is optional) ────────────


def _read_clipboard_image() -> tuple[Optional[bytes], Optional[str]]:
    """Return (png_bytes, None) or (None, error_message)."""
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        return None, "Pillow is not installed (pip install Pillow)."
    try:
        grabbed = ImageGrab.grabclipboard()
    except Exception as exc:  # clipboard access varies by platform
        return None, f"Could not read clipboard: {exc}"

    if grabbed is None:
        return None, "No image on the clipboard. Copy a screenshot first (e.g. Win+Shift+S)."
    if isinstance(grabbed, list):
        for path in grabbed:
            if isinstance(path, str) and path.lower().endswith(_IMAGE_EXTS):
                try:
                    return _normalise_to_png(Path(path).read_bytes())
                except OSError as exc:
                    return None, f"Could not read {path}: {exc}"
        return None, "Clipboard contains files but no image."
    if isinstance(grabbed, Image.Image):
        buf = io.BytesIO()
        grabbed.save(buf, format="PNG")
        return buf.getvalue(), None
    return None, "Clipboard did not contain a recognised image."


def _normalise_to_png(data: bytes) -> tuple[Optional[bytes], Optional[str]]:
    """Ensure *data* is a PNG byte string (re-encode other formats via PIL)."""
    if not data:
        return None, "Empty image data."
    if data.startswith(b"\x89PNG"):
        return data, None
    try:
        from PIL import Image
    except ImportError:
        return None, "Pillow is not installed (pip install Pillow)."
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), None
    except Exception as exc:
        return None, f"Unrecognised or corrupt image: {exc}"
