"""
docx-map-updater
-----------------
Drag-and-drop GUI for updating a figures section (e.g. an appendix of maps)
in a Word report from a folder of source images. See engine.py for the
matching/apply logic.
"""

import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

import engine

ACCENT = "#1B4F72"
BG = "#F8F9FA"
BORDER = "#DEE2E6"
MUTED = "#6C757D"
WHITE = "#FFFFFF"
GREEN = "#28A745"
RED = "#DC3545"

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_HEADER = ("Segoe UI", 14, "bold")
FONT_SMALL_ITALIC = ("Segoe UI", 8, "italic")


def _strip_braces(path_str: str) -> str:
    # tkdnd wraps paths containing spaces in {curly braces}
    path_str = path_str.strip()
    if path_str.startswith("{") and path_str.endswith("}"):
        return path_str[1:-1]
    return path_str


def _parse_dnd_paths(data: str) -> list[str]:
    paths = []
    buf = ""
    depth = 0
    for ch in data:
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            continue
        if ch == " " and depth == 0:
            if buf:
                paths.append(buf)
                buf = ""
            continue
        buf += ch
    if buf:
        paths.append(buf)
    return paths


class DropZone(tk.Frame):
    def __init__(self, parent, label_text, hint_text, on_drop_paths, **kwargs):
        super().__init__(parent, bg=WHITE, highlightbackground=BORDER,
                          highlightthickness=1, bd=0, **kwargs)
        self.on_drop_paths = on_drop_paths
        self.value = ""

        tk.Label(self, text=label_text, font=FONT_BOLD, bg=WHITE, fg=ACCENT,
                 anchor="w").pack(fill="x", padx=10, pady=(8, 0))

        self.value_label = tk.Label(self, text=hint_text, font=FONT, bg=WHITE,
                                     fg=MUTED, anchor="w", wraplength=380, justify="left")
        self.value_label.pack(fill="x", padx=10, pady=(2, 4))

        btn_row = tk.Frame(self, bg=WHITE)
        btn_row.pack(fill="x", padx=10, pady=(0, 8))
        self.browse_btn = tk.Button(btn_row, text="Browse…", font=FONT, cursor="hand2")
        self.browse_btn.pack(side="left")

        self._hint_text = hint_text

        for widget in (self, self.value_label):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._handle_drop)

    def _handle_drop(self, event):
        paths = [_strip_braces(p) for p in _parse_dnd_paths(event.data)]
        if paths:
            self.on_drop_paths(paths)

    def set_value(self, text: str):
        self.value = text
        self.value_label.config(text=text or self._hint_text, fg=(ACCENT if text else MUTED))


class App:
    def __init__(self):
        self.root = TkinterDnD.Tk()
        self.root.title("docx-map-updater")
        self.root.configure(bg=BG)
        self.root.geometry("640x760")
        self.root.minsize(560, 640)

        self.docx_path: Path | None = None
        self.images_dir: Path | None = None
        self.last_report: dict | None = None

        self._build_ui()

    # ------------------------------------------------------------------
    def _build_ui(self):
        header = tk.Frame(self.root, bg=ACCENT)
        header.pack(fill="x")
        tk.Label(header, text="docx-map-updater", bg=ACCENT, fg=WHITE,
                 font=FONT_HEADER).pack(side="left", padx=20, pady=14)

        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=18, pady=14)

        self.docx_zone = DropZone(
            main, "Word report (.docx)", "Drop a .docx file here, or Browse…",
            self._on_drop_docx,
        )
        self.docx_zone.pack(fill="x", pady=(0, 10))
        self.docx_zone.browse_btn.config(command=self._browse_docx)

        self.images_zone = DropZone(
            main, "Images folder", "Drop a folder of source images here, or Browse…",
            self._on_drop_images,
        )
        self.images_zone.pack(fill="x", pady=(0, 12))
        self.images_zone.browse_btn.config(command=self._browse_images)

        tk.Frame(main, bg=BORDER, height=1).pack(fill="x", pady=(0, 12))

        form = tk.Frame(main, bg=BG)
        form.pack(fill="x")

        self.heading_var = tk.StringVar()
        self._form_row(form, "Section heading text:", self.heading_var,
                        'e.g. "STC and Erosion Maps" — matched case-insensitively as a substring')

        self.prefix_var = tk.StringVar(value="Figure")
        self._form_row(form, "Figure caption prefix:", self.prefix_var,
                        'Captions are assumed to read like "Figure 12: description"')

        self.threshold_var = tk.StringVar(value="0.55")
        self._form_row(form, "Match threshold (0-1):", self.threshold_var,
                        "Minimum score to treat a file as a replacement rather than a new figure")

        check_row = tk.Frame(main, bg=BG)
        check_row.pack(fill="x", pady=(8, 4))

        self.apply_var = tk.BooleanVar(value=False)
        self.apply_check = tk.Checkbutton(
            check_row, text="Apply changes (unchecked = dry run / report only)",
            variable=self.apply_var, bg=BG, font=FONT, command=self._on_apply_toggled,
        )
        self.apply_check.pack(anchor="w")

        self.in_place_var = tk.BooleanVar(value=False)
        self.in_place_check = tk.Checkbutton(
            check_row, text="Overwrite in place (a .bak backup is made first)",
            variable=self.in_place_var, bg=BG, font=FONT, state="disabled",
        )
        self.in_place_check.pack(anchor="w")

        self.run_btn = tk.Button(
            main, text="Run", font=FONT_BOLD, bg=ACCENT, fg=WHITE,
            activebackground=ACCENT, activeforeground=WHITE, cursor="hand2",
            command=self._on_run, padx=16, pady=6,
        )
        self.run_btn.pack(anchor="w", pady=(8, 10))

        tk.Label(main, text="Log", font=FONT_BOLD, bg=BG, fg=ACCENT, anchor="w").pack(fill="x")

        log_outer = tk.Frame(main, bg=WHITE, highlightbackground=BORDER, highlightthickness=1)
        log_outer.pack(fill="both", expand=True, pady=(4, 0))
        self.log_text = tk.Text(log_outer, font=("Consolas", 9), bg=WHITE, fg="black",
                                 wrap="word", state="disabled", relief="flat")
        log_scroll = tk.Scrollbar(log_outer, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _form_row(self, parent, label, var, hint):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, font=FONT, bg=BG, width=22, anchor="w").pack(side="left")
        entry = tk.Entry(row, textvariable=var, font=FONT, relief="solid", bd=1)
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        tk.Label(parent, text=hint, bg=BG, fg=MUTED, font=FONT_SMALL_ITALIC,
                 justify="left", anchor="w").pack(fill="x", padx=(154, 0), pady=(0, 2))

    # ------------------------------------------------------------------
    def _on_apply_toggled(self):
        self.in_place_check.config(state="normal" if self.apply_var.get() else "disabled")
        if not self.apply_var.get():
            self.in_place_var.set(False)

    def _browse_docx(self):
        path = filedialog.askopenfilename(title="Select Word report", filetypes=[("Word documents", "*.docx")])
        if path:
            self._set_docx(path)

    def _browse_images(self):
        path = filedialog.askdirectory(title="Select images folder")
        if path:
            self._set_images(path)

    def _on_drop_docx(self, paths):
        docx_paths = [p for p in paths if p.lower().endswith(".docx")]
        if docx_paths:
            self._set_docx(docx_paths[0])
        else:
            messagebox.showwarning("docx-map-updater", "Please drop a .docx file.")

    def _on_drop_images(self, paths):
        folders = [p for p in paths if Path(p).is_dir()]
        if folders:
            self._set_images(folders[0])
            return
        files = [p for p in paths if Path(p).suffix.lower() in engine.IMAGE_EXTENSIONS]
        if files:
            self._set_images(str(Path(files[0]).parent))
        else:
            messagebox.showwarning("docx-map-updater", "Please drop a folder of images (or image files).")

    def _set_docx(self, path_str: str):
        self.docx_path = Path(path_str)
        self.docx_zone.set_value(self.docx_path.name)

    def _set_images(self, path_str: str):
        self.images_dir = Path(path_str)
        try:
            count = len(engine.list_image_files(self.images_dir))
        except Exception:
            count = 0
        self.images_zone.set_value(f"{self.images_dir}  ({count} image file(s))")

    # ------------------------------------------------------------------
    def _log(self, message: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _on_run(self):
        if self.docx_path is None:
            messagebox.showerror("docx-map-updater", "Please choose a Word report first.")
            return
        if self.images_dir is None:
            messagebox.showerror("docx-map-updater", "Please choose an images folder first.")
            return
        if not self.heading_var.get().strip():
            messagebox.showerror("docx-map-updater", "Please enter the section heading text.")
            return
        try:
            threshold = float(self.threshold_var.get())
        except ValueError:
            messagebox.showerror("docx-map-updater", "Match threshold must be a number between 0 and 1.")
            return

        self.run_btn.config(state="disabled", text="Running…")
        self._clear_log()
        threading.Thread(target=self._run_worker, args=(threshold,), daemon=True).start()

    def _run_worker(self, threshold: float):
        try:
            report = engine.run(
                docx_path=self.docx_path,
                images_dir=self.images_dir,
                heading=self.heading_var.get().strip(),
                figure_prefix=self.prefix_var.get().strip() or "Figure",
                threshold=threshold,
                apply=self.apply_var.get(),
                in_place=self.in_place_var.get(),
                log=lambda msg: self.root.after(0, self._log, msg),
            )
            self.last_report = report
        except Exception as exc:
            self.root.after(0, self._log, f"ERROR: {exc}")
            self.root.after(0, self._log, traceback.format_exc())
        finally:
            self.root.after(0, lambda: self.run_btn.config(state="normal", text="Run"))

    def run(self):
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()
