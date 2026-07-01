import os
import sys
import json
import time
import uuid
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import requests

TOPBAR_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACgAAAAoCAMAAAC7IEhfAAAAkFBMVEX//fD+9ub+8+T87t347N3z5dTx2LjuzqPqw4zczrzbuo3A"
    "vLTVqXTSnmK+qI+9nnzKlFi9hkykkn2keUl+kqV+d3CabDuGa0yCXTRsW0lnTDBqPSFXPSJMMhdBOTA+LRsxIxQiFw0RDgkKCAUG"
    "BQQEAwIDAgICAgIBAgICAQEBAQEBAQAAAQEBAAAAAAEAAACNcWI/AAAB+UlEQVR42sXU2ZKjIBQGYNcYXHBBEIn7OmIj7/92g0km"
    "nUn3pFNT1dXccPP5Iwc4mnxxaN8E12dEvMP1eZj4A8VPbua/IJ/Gaf0SLgs/z/MknsJddVVV9R9rdg/5IuuahYaue1nRS/4vqBZj"
    "CAWOYdiu75N+Wz+Hq+wIYkF0MCw/iuOAjov4FPKBRCcUhZZpuT4hUVTI5frb+2I3uMg2JkVcYcO0cYSoCm3Ocv4AG3LKfQZMA/bZ"
    "DiO2LrLvHpcWYiiYbeIq0UAeUKISCznwBtTnzBsUe92wbuMWaoZmZyqRNKOUgwcm/nafuG0txhDmFfCAbsACoVMBcdfHZiWn2z8K"
    "PvXS07Q8P9EW2wBAVpYlCUzAYqeS4y2Ry4xmGCaUUMayjBZ1liQw8G0N0vByRmc4ycrPHERpmnrH49EJm5IddN2ybMvO20spz3CW"
    "CIWO4yiXqsmhTV0AVU9gWnoi59ubUZ8wio5HN3W9VAX6rCh36ALd0iHf3qE6FcaiIEjVcBUsmxLbBsiwbR3ySxmvm5n7rj1RkmGM"
    "g8APyhoahplA3TyELV/uIB/m7a3r2rIuGaWoxJp5wE0CE9YMw+MR7rdvU7dt7Nu2xrhafo2DkNeF/764YlVDXrwSyyb5vD55rmIV"
    "fJ7nhxbyPQ3g9SYlXmx7XzRI/tNt7zd8f/erE4RKswAAAABJRU5ErkJggg=="
)

APP_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "DiscordAutoPost",
)
os.makedirs(APP_DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(APP_DATA_DIR, "discord_autopost_data.json")

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"profiles": []}

def save_data(data):
    try:
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        tmp_path = DATA_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, DATA_FILE)
    except Exception as e:
        print(f"Save error: {e}")
        try:
            messagebox.showerror(
                "Save Failed",
                f"Could not save your data:\n\n{e}\n\nFile: {DATA_FILE}",
            )
        except Exception:
            pass

def send_message(token, channel_id, content, account_type):
    try:
        if account_type == "webhook":
            r = requests.post(token, json={"content": content}, timeout=10)
        else:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bot {token}" if account_type == "bot" else token,
            }
            r = requests.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                json={"content": content}, headers=headers, timeout=10,
            )
        if r.status_code in (200, 201):
            return True, ""
        try:
            body = r.json()
        except Exception:
            body = {}
        reason = json.dumps({
            "message": body.get("message", f"HTTP {r.status_code}"),
            "code": body.get("code", r.status_code),
        })
        return False, reason
    except Exception as e:
        return False, json.dumps({"message": str(e), "code": "exception"})

def _format_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, rem_sec = divmod(seconds, 60)
    if minutes < 60:
        if rem_sec:
            return f"{minutes} minute{'s' if minutes != 1 else ''} {rem_sec} second{'s' if rem_sec != 1 else ''}"
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, rem_min = divmod(minutes, 60)
    if rem_min:
        return f"{hours} hour{'s' if hours != 1 else ''} {rem_min} minute{'s' if rem_min != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''}"

def _format_uptime(seconds):
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}d {h}h {m}m {s}s"

def _format_countdown(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s:02d}s"


C = {
    "bg":       "#0c0f17",
    "sidebar":  "#0f131e",
    "panel":    "#171d28",
    "card":     "#1e2636",
    "input":    "#141c28",
    "border":   "#293549",
    "accent":   "#7289da",
    "accent2":  "#5e79d1",
    "fg":       "#e8ebf5",
    "fg2":      "#9aa0bf",
    "fg3":      "#7a86a4",
    "success":  "#43b581",
    "danger":   "#f04747",
    "warn":     "#faa61a",
    "green":    "#57f287",
    "red":      "#f04747",
    "scrollbar":"#3c4a70",
}

FONT      = ("Segoe UI", 9)
FONT_B    = ("Segoe UI", 9, "bold")
FONT_SM   = ("Segoe UI", 8)
FONT_LG   = ("Segoe UI", 13, "bold")
FONT_XL   = ("Segoe UI", 11, "bold")
MONO      = ("Consolas", 9)

def entry(parent, textvariable=None, show=None, width=None, placeholder=None):
    e = tk.Entry(
        parent, bg=C["input"], fg=C["fg"], insertbackground=C["fg"],
        relief="flat", font=FONT, bd=0,
        highlightthickness=1, highlightbackground=C["border"], highlightcolor=C["accent"],
        **({"textvariable": textvariable} if textvariable else {}),
        **({"show": show} if show else {}),
        **({"width": width} if width else {}),
    )
    if placeholder and textvariable:
        def _on_focus_in(ev):
            if textvariable.get() == placeholder:
                textvariable.set("")
                e.config(fg=C["fg"])
        def _on_focus_out(ev):
            if textvariable.get() == "":
                textvariable.set(placeholder)
                e.config(fg=C["fg3"])
        textvariable.set(placeholder)
        e.config(fg=C["fg3"])
        e.bind("<FocusIn>",  _on_focus_in)
        e.bind("<FocusOut>", _on_focus_out)
    return e

def btn(parent, text, command, color=None, fg=None, width=None, small=False):
    bg = color or C["accent"]
    f  = fg or C["fg"]
    b = tk.Button(
        parent, text=text, command=command,
        bg=bg, fg=f, activebackground=bg, activeforeground=f,
        relief="flat", font=FONT_SM if small else FONT_B,
        padx=8 if small else 12, pady=3 if small else 6,
        cursor="hand2", bd=0, overrelief="flat",
    )
    if width:
        b.config(width=width)
    b.bind("<Enter>", lambda e: b.config(bg=_darken(bg)))
    b.bind("<Leave>", lambda e: b.config(bg=bg))
    return b

def _darken(hex_color):
    h = hex_color.lstrip("#")
    r, g, b_ = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
    r,g,b_ = max(0,r-20), max(0,g-20), max(0,b_-20)
    return f"#{r:02x}{g:02x}{b_:02x}"

def label(parent, text, fg=None, font=None, anchor="w", **kw):
    return tk.Label(parent, text=text, bg=parent["bg"], fg=fg or C["fg"],
                    font=font or FONT, anchor=anchor, **kw)

def sep(parent):
    f = tk.Frame(parent, bg=C["border"], height=1)
    f.pack(fill="x", pady=4)
    return f

def card(parent, **kw):
    return tk.Frame(parent, bg=C["card"],
                    highlightthickness=1, highlightbackground=C["border"], **kw)

_SCROLL_CANVASES = []

def _find_scroll_canvas(widget):
    while widget is not None:
        if isinstance(widget, tk.Canvas) and widget in _SCROLL_CANVASES:
            return widget
        widget = getattr(widget, "master", None)
    return None


def _mousewheel_steps(delta):
    steps = int(delta / 120)
    return steps if steps != 0 else (1 if delta > 0 else -1)


def _on_mousewheel(ev):
    canvas = _find_scroll_canvas(ev.widget)
    if not canvas:
        return
    try:
        lo, hi = canvas.yview()
    except tk.TclError:
        return
    if lo <= 0.0 and hi >= 1.0:
        return
    canvas.yview_scroll(-_mousewheel_steps(ev.delta), "units")
    return "break"

class ScrollFrame(tk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=kw.get("bg", C["panel"]))
        canvas = tk.Canvas(self, bg=kw.get("bg", C["panel"]), bd=0,
                           highlightthickness=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview,
                           style="Thin.Vertical.TScrollbar")
        self.inner = tk.Frame(canvas, bg=kw.get("bg", C["panel"]))
        win_id = canvas.create_window((0,0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width), add="+")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        _SCROLL_CANVASES.append(canvas)
        self.canvas = canvas

class BoundedScrollFrame(tk.Frame):
    def __init__(self, parent, height=160, style="ThinCard.Vertical.TScrollbar", **kw):
        bg = kw.get("bg", C["card"])
        super().__init__(parent, bg=bg)
        canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0, height=height)
        sb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview, style=style)
        self.inner = tk.Frame(canvas, bg=bg)
        win_id = canvas.create_window((0,0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width), add="+")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        _SCROLL_CANVASES.append(canvas)
        self.canvas = canvas

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gio's Discord Autopost")
        self.configure(bg=C["bg"])
        self.geometry("1050x660")
        self.minsize(860, 540)

        self._data           = load_data()
        self._active_id      = None
        self._stop_events    = {}
        self._threads        = {}
        self._log_widgets    = {}
        self._log_buffers    = {}
        self._status_labels  = {}
        self._start_times    = {}
        self._next_send_at   = {}
        self._channel_next_send_at = {}
        self._stat_widgets   = {}
        self._routing_refreshers = {}
        self._toggle_btn     = None
        self._sidebar_visible = True

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Vertical.TScrollbar", background=C["input"],
                        troughcolor=C["panel"], bordercolor=C["panel"],
                        arrowcolor=C["fg2"], relief="flat")

        for name, trough in (("Thin.Vertical.TScrollbar", C["panel"]),
                              ("ThinCard.Vertical.TScrollbar", C["card"]),
                              ("ThinDark.Vertical.TScrollbar", C["bg"])):
            style.layout(name, [
                ("Vertical.Scrollbar.trough", {
                    "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})],
                    "sticky": "ns",
                }),
            ])
            style.configure(name, gripcount=0, width=7, borderwidth=0,
                            background=C["scrollbar"], troughcolor=trough,
                            bordercolor=trough, lightcolor=C["scrollbar"],
                            darkcolor=C["scrollbar"], arrowsize=0, relief="flat")
            style.map(name, background=[("active", C["fg2"])])

        self._build()
        self.bind_all("<MouseWheel>", _on_mousewheel)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_sidebar()
        self.after(1000, self._tick_active_profile_ui)

    def _set_channel_next_send(self, pid, next_map):
        self._channel_next_send_at[pid] = next_map
        times = [t for t in next_map.values() if isinstance(t, (int, float))]
        if times:
            self._next_send_at[pid] = min(times)
        else:
            self._next_send_at.pop(pid, None)

    def _format_channel_next_send_text(self, p, pid):
        next_map = self._channel_next_send_at.get(pid, {})
        if not next_map:
            return "—"
        now = time.time()
        lines = []
        for ch in p.get("channels", []):
            ts = next_map.get(ch["id"])
            if ts is None:
                lines.append(f"{ch['label']}: —")
            else:
                lines.append(f"{ch['label']}: {_format_countdown(max(0, ts - now))}")
        return "\n".join(lines)

    def _build(self):
        bar = tk.Frame(self, bg=C["sidebar"], height=48)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(bar, text="  \U0001f43e  GIO'S DISCORD AUTOPOST",
                 bg=C["sidebar"], fg=C["fg"], font=FONT_LG).pack(side="left", padx=12, pady=10)
        tk.Label(bar, text="Manage profiles & autopost to Discord",
                 bg=C["sidebar"], fg=C["fg2"], font=FONT_SM).pack(side="left")
        self._sidebar_toggle_btn = tk.Button(
            bar, text="Hide Sidebar", command=self._toggle_sidebar,
            bg=C["accent2"], fg=C["fg"], activebackground=C["accent2"],
            activeforeground=C["fg"], relief="flat", font=FONT_SM,
            padx=10, pady=4, cursor="hand2", bd=0)
        self._sidebar_toggle_btn.pack(side="right", padx=(0,6), pady=8)
        self._topbar_icon = tk.PhotoImage(data=TOPBAR_ICON_B64)
        tk.Label(bar, image=self._topbar_icon, bg=C["sidebar"]).pack(side="right", padx=14)

        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill="both", expand=True)

        self._sidebar = tk.Frame(body, bg=C["sidebar"], width=220)
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)

        sh = tk.Frame(self._sidebar, bg=C["sidebar"])
        sh.pack(fill="x", padx=10, pady=(12,6))
        tk.Label(sh, text="PROFILES", bg=C["sidebar"], fg=C["fg2"],
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        btn(sh, "+", self._new_profile, color=C["accent"], small=True).pack(side="right")

        sep(self._sidebar)

        self._profile_list_frame = tk.Frame(self._sidebar, bg=C["sidebar"])
        self._profile_list_frame.pack(fill="both", expand=True, padx=8)

        self._content = tk.Frame(body, bg=C["panel"])
        self._content.pack(side="left", fill="both", expand=True)

        self._show_empty()

    def _show_empty(self):
        for w in self._content.winfo_children():
            w.destroy()
        f = tk.Frame(self._content, bg=C["panel"])
        f.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(f, text="\U0001f43e", bg=C["panel"], fg=C["fg3"],
                 font=("Segoe UI", 48)).pack()
        tk.Label(f, text="No profile selected",
                 bg=C["panel"], fg=C["fg2"], font=FONT_XL).pack(pady=(0,4))
        tk.Label(f, text='Click  +  in the sidebar to create one',
                 bg=C["panel"], fg=C["fg3"], font=FONT).pack()

    def _refresh_sidebar(self):
        for w in self._profile_list_frame.winfo_children():
            w.destroy()
        for p in self._data["profiles"]:
            self._make_profile_row(p)

    def _toggle_sidebar(self):
        self._sidebar_visible = not self._sidebar_visible
        if self._sidebar_visible:
            self._sidebar.pack(side="left", fill="y")
            self._sidebar_toggle_btn.config(text="Hide Sidebar")
        else:
            self._sidebar.pack_forget()
            self._sidebar_toggle_btn.config(text="Show Sidebar")
        self._refresh_sidebar()

    def _make_profile_row(self, p):
        pid  = p["id"]
        active = pid == self._active_id
        running = pid in self._stop_events and not self._stop_events[pid].is_set()
        base_bg = C["accent2"] if active else C["sidebar"]

        row = tk.Frame(self._profile_list_frame, bg=base_bg, cursor="hand2")
        row.pack(fill="x", pady=2)

        dot_color = C["green"] if running else C["fg3"]
        dot_lbl = tk.Label(row, text="\u25cf", bg=base_bg, fg=dot_color, font=("Segoe UI", 8))
        dot_lbl.pack(side="left", padx=(8,4), pady=8)
        txt_lbl = tk.Label(row, text=p["label"], bg=base_bg, fg=C["fg"],
                           font=FONT_B if active else FONT, anchor="w")
        txt_lbl.pack(side="left", fill="x", expand=True, pady=8)

        del_b = tk.Button(row, text="\u2715", bg=base_bg, fg=C["fg3"],
                          activebackground=C["danger"], activeforeground=C["fg"],
                          relief="flat", font=FONT_SM, cursor="hand2", bd=0,
                          command=lambda _id=pid: self._delete_profile(_id))
        del_b.pack(side="right", padx=6)

        if not active:
            hover_bg = C["input"]
            def _on_enter(e):
                row.config(bg=hover_bg)
                dot_lbl.config(bg=hover_bg)
                txt_lbl.config(bg=hover_bg)
                del_b.config(bg=hover_bg)
            def _on_leave(e):
                row.config(bg=base_bg)
                dot_lbl.config(bg=base_bg)
                txt_lbl.config(bg=base_bg)
                del_b.config(bg=base_bg)
            row.bind("<Enter>", _on_enter)
            row.bind("<Leave>", _on_leave)

        row.bind("<Button-1>", lambda e, _id=pid: self._select_profile(_id))
        for child in row.winfo_children():
            if child is not del_b:
                child.bind("<Button-1>", lambda e, _id=pid: self._select_profile(_id))

    def _new_profile(self):
        win = tk.Toplevel(self)
        win.title("New Profile")
        win.configure(bg=C["panel"])
        win.geometry("340x140")
        win.resizable(False, False)
        win.grab_set()

        tk.Label(win, text="Profile Name", bg=C["panel"], fg=C["fg2"],
                 font=FONT_SM).pack(anchor="w", padx=16, pady=(16,2))
        var = tk.StringVar()
        e = entry(win, textvariable=var)
        e.pack(fill="x", padx=16, ipady=5)
        e.focus_set()

        def _create():
            name = var.get().strip()
            if not name:
                return
            p = {
                "id": str(uuid.uuid4()),
                "label": name,
                "accounts": [],
                "channels": [],
                "messages": [],
                "routing_disabled": {},
                "delay": 5,
            }
            self._data["profiles"].append(p)
            save_data(self._data)
            win.destroy()
            self._refresh_sidebar()
            self._select_profile(p["id"])

        e.bind("<Return>", lambda ev: _create())
        btn(win, "Create Profile", _create).pack(pady=12, padx=16, fill="x", ipady=3)

    def _delete_profile(self, pid):
        p = self._get_profile(pid)
        if not p:
            return
        if not messagebox.askyesno("Delete Profile",
                f"Delete '{p['label']}'?\nThis cannot be undone."):
            return
        if pid in self._stop_events:
            self._stop_events[pid].set()
        self._data["profiles"] = [x for x in self._data["profiles"] if x["id"] != pid]
        save_data(self._data)
        self._log_buffers.pop(pid, None)
        self._log_widgets.pop(pid, None)
        self._next_send_at.pop(pid, None)
        self._stat_widgets.pop(pid, None)
        self._routing_refreshers.pop(pid, None)
        if self._active_id == pid:
            self._active_id = None
            self._show_empty()
        self._refresh_sidebar()

    def _select_profile(self, pid):
        self._active_id = pid
        self._refresh_sidebar()
        self._show_profile(pid)

    def _get_profile(self, pid):
        return next((p for p in self._data["profiles"] if p["id"] == pid), None)

    def _show_profile(self, pid):
        p = self._get_profile(pid)
        if not p:
            return
        for w in self._content.winfo_children():
            w.destroy()

        hdr = tk.Frame(self._content, bg=C["panel"])
        hdr.pack(fill="x", padx=20, pady=(16, 12))

        lbl_var = tk.StringVar(value=p["label"])
        lbl_entry = tk.Entry(hdr, textvariable=lbl_var, bg=C["panel"], fg=C["fg"],
                             font=FONT_LG, relief="flat", bd=0,
                             insertbackground=C["fg"], highlightthickness=0)
        lbl_entry.pack(side="left")
        def _save_label(ev=None):
            name = lbl_var.get().strip()
            if name:
                p["label"] = name
                save_data(self._data)
                self._refresh_sidebar()
        lbl_entry.bind("<FocusOut>", _save_label)
        lbl_entry.bind("<Return>",   _save_label)

        self._status_labels[pid] = tk.Label(
            hdr, text="\u25cf Stopped", bg=C["panel"], fg=C["red"], font=FONT_B)
        self._status_labels[pid].pack(side="left", padx=(16,0))

        running = pid in self._stop_events and not self._stop_events[pid].is_set()
        if running:
            self._status_labels[pid].config(text="\u25cf Running", fg=C["green"])

        sep_h = tk.Frame(self._content, bg=C["border"], height=1)
        sep_h.pack(fill="x", padx=20)

        scroll = ScrollFrame(self._content, bg=C["panel"])
        scroll.pack(fill="both", expand=True, padx=20, pady=(12,16))
        body = scroll.inner

        self._section_accounts(body, p, pid)
        self._section_channels(body, p, pid)
        self._section_messages(body, p, pid)
        self._section_routing(body, p, pid)
        self._section_action(body, p, pid)
        self._section_status(body, p, pid)

    def _section_title(self, parent, icon, text):
        row = tk.Frame(parent, bg=C["panel"])
        row.pack(fill="x", pady=(18,10))
        stripe = tk.Frame(row, bg=C["accent"], width=3, height=22)
        stripe.pack(side="left", fill="y", padx=(0,9), pady=2)
        tk.Label(row, text=icon, bg=C["panel"], fg=C["fg"], font=FONT_LG).pack(side="left")
        tk.Label(row, text=text.upper(), bg=C["panel"], fg=C["fg2"], font=FONT_B).pack(side="left", padx=(7,0))
        return row

    def _section_accounts(self, parent, p, pid):
        self._section_title(parent, "\U0001f9d1\u200d\U0001f4bb", "Accounts")
        c = card(parent)
        c.pack(fill="x", pady=(0, 12))

        LABEL_W, TYPE_W = 180, 90
        cols = ("Label", "Type", "Token")
        tv = ttk.Treeview(c, columns=cols, show="headings", height=4)
        style = ttk.Style()
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Treeview", background=C["card"], foreground=C["fg"],
                        rowheight=26, fieldbackground=C["card"], font=FONT,
                        borderwidth=0, relief="flat")
        style.configure("Treeview.Heading", background=C["input"], foreground=C["fg2"],
                        font=FONT_B, relief="flat", borderwidth=0)
        style.map("Treeview", background=[("selected", C["accent2"])])

        for col, w in zip(cols, [LABEL_W, TYPE_W, 280]):
            tv.heading(col, text=col)
            tv.column(col, width=w, stretch=col=="Token")
        for a in p["accounts"]:
            masked = a["token"][:10] + "..." + a["token"][-4:] if len(a["token"]) > 14 else a["token"]
            tv.insert("", "end", iid=a["id"],
                      values=(a["label"], a["type"], masked))
        tv.pack(fill="both", expand=True, padx=1, pady=1)

        sep_inner = tk.Frame(c, bg=C["border"], height=1)
        sep_inner.pack(fill="x")

        add = tk.Frame(c, bg=C["card"])
        add.pack(fill="x")
        add.columnconfigure(0, minsize=LABEL_W)
        add.columnconfigure(1, minsize=TYPE_W)
        add.columnconfigure(2, weight=1)

        label_col = tk.Frame(add, bg=C["card"],
                             highlightthickness=1, highlightbackground=C["border"])
        label_col.grid(row=0, column=0, sticky="new", padx=(10,6), pady=10)
        inner_l = tk.Frame(label_col, bg=C["card"])
        inner_l.pack(fill="x", padx=8, pady=7)
        tk.Label(inner_l, text="Label", bg=C["card"], fg=C["fg2"], font=FONT_SM).pack(anchor="w")
        lv = tk.StringVar()
        entry(inner_l, textvariable=lv).pack(fill="x", pady=(4,0))

        type_col = tk.Frame(add, bg=C["card"],
                            highlightthickness=1, highlightbackground=C["border"])
        type_col.grid(row=0, column=1, sticky="new", padx=6, pady=10)
        inner_t = tk.Frame(type_col, bg=C["card"])
        inner_t.pack(fill="x", padx=8, pady=7)
        tk.Label(inner_t, text="Type", bg=C["card"], fg=C["fg2"], font=FONT_SM).pack(anchor="w")
        type_v = tk.StringVar(value="user")
        radio_wrap = tk.Frame(inner_t, bg=C["card"])
        radio_wrap.pack(anchor="w", pady=(4,0))
        for val, txt in [("user","User"),("bot","Bot"),("webhook","Webhook")]:
            tk.Radiobutton(radio_wrap, text=txt, variable=type_v, value=val,
                           bg=C["card"], fg=C["fg"], selectcolor=C["input"],
                           activebackground=C["card"], font=FONT_SM).pack(anchor="w")

        token_col = tk.Frame(add, bg=C["card"],
                             highlightthickness=1, highlightbackground=C["border"])
        token_col.grid(row=0, column=2, sticky="new", padx=(6,10), pady=10)
        inner_tok = tk.Frame(token_col, bg=C["card"])
        inner_tok.pack(fill="x", padx=8, pady=7)
        tk.Label(inner_tok, text="Token", bg=C["card"], fg=C["fg2"], font=FONT_SM).pack(anchor="w")
        tv2 = tk.StringVar()
        entry(inner_tok, textvariable=tv2, show="\u2022").pack(fill="x", pady=(4,0))

        selected_account_id = [None]
        def _on_account_select(ev=None):
            sel = tv.selection()
            if len(sel) != 1:
                selected_account_id[0] = None
                return
            selected_account_id[0] = sel[0]
            a = next((x for x in p["accounts"] if x["id"] == sel[0]), None)
            if a:
                lv.set(a["label"])
                type_v.set(a["type"])
                tv2.set(a["token"])
        tv.bind("<<TreeviewSelect>>", _on_account_select)

        def _add():
            lbl = lv.get().strip(); tok = tv2.get().strip()
            if not lbl or not tok:
                messagebox.showwarning("Missing fields", "Label and Token are required.")
                return
            if selected_account_id[0]:
                a = next((x for x in p["accounts"] if x["id"] == selected_account_id[0]), None)
                if a:
                    a["label"] = lbl
                    a["token"] = tok
                    a["type"] = type_v.get()
                    save_data(self._data)
                    masked = tok[:10] + "..." + tok[-4:] if len(tok) > 14 else tok
                    tv.item(a["id"], values=(lbl, a["type"], masked))
                    selected_account_id[0] = None
                    tv.selection_remove(tv.selection())
                    lv.set(""); tv2.set("")
                    return
            a = {"id": str(uuid.uuid4()), "label": lbl, "token": tok, "type": type_v.get()}
            p["accounts"].append(a)
            save_data(self._data)
            masked = tok[:10] + "..." + tok[-4:] if len(tok) > 14 else tok
            tv.insert("", "end", iid=a["id"], values=(lbl, type_v.get(), masked))
            lv.set(""); tv2.set("")
        def _update():
            if not selected_account_id[0]:
                messagebox.showwarning("No selection", "Select an account to update.")
                return
            a = next((x for x in p["accounts"] if x["id"] == selected_account_id[0]), None)
            if not a:
                return
            lbl = lv.get().strip(); tok = tv2.get().strip()
            if not lbl or not tok:
                messagebox.showwarning("Missing fields", "Label and Token are required.")
                return
            a["label"] = lbl
            a["token"] = tok
            a["type"] = type_v.get()
            save_data(self._data)
            masked = tok[:10] + "..." + tok[-4:] if len(tok) > 14 else tok
            tv.item(a["id"], values=(lbl, a["type"], masked))
            selected_account_id[0] = None
            tv.selection_remove(tv.selection())
            lv.set(""); tv2.set("")
        def _del():
            sel = tv.selection()
            if not sel: return
            p["accounts"] = [a for a in p["accounts"] if a["id"] not in sel]
            save_data(self._data)
            for s in sel: tv.delete(s)
            selected_account_id[0] = None
            lv.set(""); tv2.set("")

        btn_row = tk.Frame(add, bg=C["card"])
        btn_row.grid(row=1, column=0, columnspan=3, padx=10, pady=(2,10), sticky="w")
        btn(btn_row, "+ Add / Save Account", _add, small=True).pack(side="left", padx=(0,6))
        btn(btn_row, "Update Selected", _update, small=True).pack(side="left", padx=(0,6))
        btn(btn_row, "\u2715 Remove Selected", _del, color=C["danger"], small=True).pack(side="left")

    def _section_channels(self, parent, p, pid):
        self._section_title(parent, "\U0001f4e1", "Channels")
        c = card(parent)
        c.pack(fill="x", pady=(0, 12))

        CHLABEL_W = 180
        cols = ("Label", "Channel ID", "Delay")
        tv = ttk.Treeview(c, columns=cols, show="headings", height=4)
        for col, w in zip(cols, [CHLABEL_W, 280, 80]):
            tv.heading(col, text=col)
            tv.column(col, width=w, stretch=col=="Channel ID")
        for ch in p["channels"]:
            tv.insert("", "end", iid=ch["id"], values=(ch["label"], ch["channel_id"], str(ch.get("delay", 5))))
        tv.pack(fill="both", expand=True, padx=1, pady=1)

        sep_inner = tk.Frame(c, bg=C["border"], height=1)
        sep_inner.pack(fill="x")

        add = tk.Frame(c, bg=C["card"])
        add.pack(fill="x")
        add.columnconfigure(0, minsize=CHLABEL_W)
        add.columnconfigure(1, weight=1)

        label_col = tk.Frame(add, bg=C["card"],
                             highlightthickness=1, highlightbackground=C["border"])
        label_col.grid(row=0, column=0, sticky="new", padx=(10,6), pady=10)
        inner_l = tk.Frame(label_col, bg=C["card"])
        inner_l.pack(fill="x", padx=8, pady=7)
        tk.Label(inner_l, text="Label", bg=C["card"], fg=C["fg2"], font=FONT_SM).pack(anchor="w")
        lv = tk.StringVar()
        entry(inner_l, textvariable=lv).pack(fill="x", pady=(4,0))

        cid_col = tk.Frame(add, bg=C["card"],
                           highlightthickness=1, highlightbackground=C["border"])
        cid_col.grid(row=0, column=1, sticky="new", padx=(6,10), pady=10)
        inner_c = tk.Frame(cid_col, bg=C["card"])
        inner_c.pack(fill="x", padx=8, pady=7)
        tk.Label(inner_c, text="Channel ID", bg=C["card"], fg=C["fg2"], font=FONT_SM).pack(anchor="w")
        cv = tk.StringVar()
        entry(inner_c, textvariable=cv).pack(fill="x", pady=(4,0))

        delay_col = tk.Frame(add, bg=C["card"],
                             highlightthickness=1, highlightbackground=C["border"])
        delay_col.grid(row=0, column=2, sticky="new", padx=(6,10), pady=10)
        inner_d = tk.Frame(delay_col, bg=C["card"])
        inner_d.pack(fill="x", padx=8, pady=7)
        tk.Label(inner_d, text="Delay", bg=C["card"], fg=C["fg2"], font=FONT_SM).pack(anchor="w")
        dv = tk.StringVar(value="5")
        entry(inner_d, textvariable=dv, width=8).pack(fill="x", pady=(4,0))

        selected_channel_id = [None]
        def _on_channel_select(ev=None):
            sel = tv.selection()
            if len(sel) != 1:
                selected_channel_id[0] = None
                return
            selected_channel_id[0] = sel[0]
            ch = next((x for x in p["channels"] if x["id"] == sel[0]), None)
            if ch:
                lv.set(ch["label"])
                cv.set(ch["channel_id"])
                dv.set(str(ch.get("delay", 5)))
        tv.bind("<<TreeviewSelect>>", _on_channel_select)

        def _add():
            lbl = lv.get().strip(); cid = cv.get().strip(); delay = dv.get().strip()
            if not lbl or not cid:
                messagebox.showwarning("Missing fields", "Label, Channel ID, and delay are required.")
                return
            try:
                delay_val = float(delay)
            except ValueError:
                messagebox.showwarning("Invalid delay", "Channel delay must be a number.")
                return
            if selected_channel_id[0]:
                ch = next((x for x in p["channels"] if x["id"] == selected_channel_id[0]), None)
                if ch:
                    ch["label"] = lbl
                    ch["channel_id"] = cid
                    ch["delay"] = delay_val
                    save_data(self._data)
                    tv.item(ch["id"], values=(lbl, cid, str(delay_val)))
                    selected_channel_id[0] = None
                    tv.selection_remove(tv.selection())
                    lv.set(""); cv.set(""); dv.set("5")
                    self._routing_refreshers.get(pid, lambda: None)()
                    return
            ch = {"id": str(uuid.uuid4()), "label": lbl, "channel_id": cid, "delay": delay_val}
            p["channels"].append(ch)
            save_data(self._data)
            tv.insert("", "end", iid=ch["id"], values=(lbl, cid, str(delay_val)))
            lv.set(""); cv.set(""); dv.set("5")
            self._routing_refreshers.get(pid, lambda: None)()

        def _get_channel_delay(ch):
            return ch.get("delay", 5)

        def _update():
            if not selected_channel_id[0]:
                messagebox.showwarning("No selection", "Select a channel to update.")
                return
            ch = next((x for x in p["channels"] if x["id"] == selected_channel_id[0]), None)
            if not ch:
                return
            lbl = lv.get().strip(); cid = cv.get().strip(); delay = dv.get().strip()
            if not lbl or not cid:
                messagebox.showwarning("Missing fields", "Label, Channel ID, and delay are required.")
                return
            try:
                delay_val = float(delay)
            except ValueError:
                messagebox.showwarning("Invalid delay", "Channel delay must be a number.")
                return
            ch["label"] = lbl
            ch["channel_id"] = cid
            ch["delay"] = delay_val
            save_data(self._data)
            tv.item(ch["id"], values=(lbl, cid, str(delay_val)))
            self._routing_refreshers.get(pid, lambda: None)()
            selected_channel_id[0] = None
            tv.selection_remove(tv.selection())
            lv.set(""); cv.set(""); dv.set("5")

        def _remove():
            sel = tv.selection()
            if not sel:
                return
            p["channels"] = [ch for ch in p["channels"] if ch["id"] not in sel]
            for cid in sel:
                p.get("routing_disabled", {}).pop(cid, None)
                tv.delete(cid)
            save_data(self._data)
            selected_channel_id[0] = None
            lv.set(""); cv.set(""); dv.set("5")
            self._routing_refreshers.get(pid, lambda: None)()
        def _del():
            sel = tv.selection()
            if not sel: return
            p["channels"] = [ch for ch in p["channels"] if ch["id"] not in sel]
            for cid in sel:
                p.get("routing_disabled", {}).pop(cid, None)
            save_data(self._data)
            for s in sel: tv.delete(s)
            self._routing_refreshers.get(pid, lambda: None)()

        btn_row = tk.Frame(add, bg=C["card"])
        btn_row.grid(row=1, column=0, columnspan=3, padx=10, pady=(2,10), sticky="w")
        btn(btn_row, "+ Add / Save Channel", _add, small=True).pack(side="left", padx=(0,6))
        btn(btn_row, "Update Selected", _update, small=True).pack(side="left", padx=(0,6))
        btn(btn_row, "\u2715 Remove Selected", _del, color=C["danger"], small=True).pack(side="left")

    def _section_messages(self, parent, p, pid):
        self._section_title(parent, "\u2709\ufe0f", "Messages")
        c = card(parent)
        c.pack(fill="x", pady=(0, 12))

        list_wrap = tk.Frame(c, bg=C["card"])
        list_wrap.pack(fill="x", padx=8, pady=8)
        scroll = BoundedScrollFrame(list_wrap, height=220, bg=C["card"])
        scroll.pack(fill="x")

        MSG_MIN_CARD_W = 170
        MSG_MAX_COLS = 3
        grid_state = {"cards": []}

        def _compute_cols():
            avail = scroll.canvas.winfo_width()
            if avail <= 1:
                return MSG_MAX_COLS
            return max(1, min(MSG_MAX_COLS, avail // MSG_MIN_CARD_W))

        def _regrid(ev=None):
            cols = _compute_cols()
            for i in range(MSG_MAX_COLS):
                scroll.inner.columnconfigure(i, weight=1 if i < cols else 0)
            for idx, row in enumerate(grid_state["cards"]):
                row.grid(row=idx // cols, column=idx % cols, sticky="nsew", padx=4, pady=4)

        def _render_rows():
            for w in scroll.inner.winfo_children():
                w.destroy()
            grid_state["cards"] = []
            if not p["messages"]:
                empty = tk.Label(scroll.inner, text="No messages yet — add one below.",
                                 bg=C["card"], fg=C["fg3"], font=FONT_SM)
                empty.grid(row=0, column=0, sticky="w", padx=6, pady=10)
                return
            for m in p["messages"]:
                row = tk.Frame(scroll.inner, bg=C["input"],
                               highlightthickness=1, highlightbackground=C["border"])
                row.columnconfigure(0, weight=1)

                lbl = tk.Label(row, text=m["content"], bg=C["input"], fg=C["fg"],
                              font=MONO, justify="left", anchor="w")
                lbl.grid(row=0, column=0, sticky="ew", padx=(10,4), pady=8)
                def _update_wrap(ev, lbl=lbl):
                    lbl.config(wraplength=max(70, ev.width - 36))
                row.bind("<Configure>", _update_wrap)

                def _del(mid=m["id"]):
                    p["messages"] = [x for x in p["messages"] if x["id"] != mid]
                    for lst in p.get("routing_disabled", {}).values():
                        if mid in lst:
                            lst.remove(mid)
                    save_data(self._data)
                    _render_rows()
                    self._routing_refreshers.get(pid, lambda: None)()
                delbtn = tk.Label(row, text="\u2715", bg=C["input"], fg=C["fg3"],
                                  font=FONT_B, cursor="hand2")
                delbtn.grid(row=0, column=1, sticky="ne", padx=(0,8), pady=8)
                delbtn.bind("<Button-1>", lambda e, f=_del: f())
                delbtn.bind("<Enter>", lambda e, w=delbtn: w.config(fg=C["red"]))
                delbtn.bind("<Leave>", lambda e, w=delbtn: w.config(fg=C["fg3"]))

                grid_state["cards"].append(row)
            _regrid()

        scroll.canvas.bind("<Configure>", _regrid, add="+")
        _render_rows()

        sep_inner = tk.Frame(c, bg=C["border"], height=1)
        sep_inner.pack(fill="x")

        add = tk.Frame(c, bg=C["card"])
        add.pack(fill="x")

        tk.Label(add, text="Message content:", bg=C["card"], fg=C["fg2"],
                 font=FONT_SM).pack(anchor="w", padx=10, pady=(8,2))
        txt_frame = tk.Frame(add, bg=C["card"])
        txt_frame.pack(fill="x", padx=10, pady=(0,4))
        msg_box = tk.Text(txt_frame, bg=C["input"], fg=C["fg"], insertbackground=C["fg"],
                          relief="flat", font=MONO, height=5, wrap="word",
                          highlightthickness=1, highlightbackground=C["border"],
                          highlightcolor=C["accent"])
        msg_box.pack(fill="x")

        MSG_BOX_MIN_H, MSG_BOX_MAX_H = 5, 14
        editing_message_id = [None]
        editing_label = tk.Label(add, text="", bg=C["card"], fg=C["accent"], font=FONT_SM)
        editing_label.pack(anchor="w", padx=10, pady=(4,0))

        def _autosize_msg_box(ev=None):
            try:
                lines = msg_box.count("1.0", "end", "displaylines")
                lines = lines[0] if isinstance(lines, (tuple, list)) else (lines or 1)
            except tk.TclError:
                lines = 1
            h = max(MSG_BOX_MIN_H, min(lines, MSG_BOX_MAX_H))
            if int(msg_box.cget("height")) != h:
                msg_box.config(height=h)
        msg_box.bind("<KeyRelease>", _autosize_msg_box)

        def _set_editing(mid=None):
            editing_message_id[0] = mid
            if mid is None:
                editing_label.config(text="")
                add_btn.config(text="+ Add  (Ctrl+Enter)")
                msg_box.delete("1.0", "end")
                msg_box.config(height=MSG_BOX_MIN_H)
                return
            m = next((x for x in p["messages"] if x["id"] == mid), None)
            if not m:
                editing_label.config(text="")
                add_btn.config(text="+ Add  (Ctrl+Enter)")
                return
            editing_label.config(text="Editing selected message.")
            add_btn.config(text="Save Message")
            msg_box.delete("1.0", "end")
            msg_box.insert("1.0", m["content"])
            msg_box.config(height=min(MSG_BOX_MAX_H, max(MSG_BOX_MIN_H, len(m["content"].splitlines()))))

        def _add():
            content = msg_box.get("1.0", "end-1c").strip()
            if not content:
                return
            if editing_message_id[0]:
                m = next((x for x in p["messages"] if x["id"] == editing_message_id[0]), None)
                if m:
                    m["content"] = content
                    save_data(self._data)
                    _render_rows()
                _set_editing(None)
                self._routing_refreshers.get(pid, lambda: None)()
                return
            m = {"id": str(uuid.uuid4()), "content": content}
            p["messages"].append(m)
            save_data(self._data)
            _render_rows()
            msg_box.delete("1.0", "end")
            msg_box.config(height=MSG_BOX_MIN_H)
            self._routing_refreshers.get(pid, lambda: None)()

        def _cancel_edit():
            _set_editing(None)

        def _edit(mid):
            _set_editing(mid)

        msg_box.bind("<Control-Return>", lambda e: _add())
        btn_row = tk.Frame(add, bg=C["card"])
        btn_row.pack(fill="x", padx=10, pady=(0,8))
        add_btn = btn(btn_row, "+ Add  (Ctrl+Enter)", _add, small=True)
        add_btn.pack(side="left")
        btn(btn_row, "Cancel Edit", _cancel_edit, color=C["danger"], small=True).pack(side="left", padx=(6,0))

        def _render_rows():
            for w in scroll.inner.winfo_children():
                w.destroy()
            grid_state["cards"] = []
            if not p["messages"]:
                empty = tk.Label(scroll.inner, text="No messages yet — add one below.",
                                 bg=C["card"], fg=C["fg3"], font=FONT_SM)
                empty.grid(row=0, column=0, sticky="w", padx=6, pady=10)
                return
            for m in p["messages"]:
                row = tk.Frame(scroll.inner, bg=C["input"],
                               highlightthickness=1, highlightbackground=C["border"])
                row.columnconfigure(0, weight=1)

                lbl = tk.Label(row, text=m["content"], bg=C["input"], fg=C["fg"],
                              font=MONO, justify="left", anchor="w", wraplength=240)
                lbl.grid(row=0, column=0, sticky="ew", padx=(10,4), pady=8)
                def _update_wrap(ev, lbl=lbl):
                    lbl.config(wraplength=max(70, ev.width - 36))
                row.bind("<Configure>", _update_wrap)

                editbtn = tk.Label(row, text="✎", bg=C["input"], fg=C["fg3"],
                                  font=FONT_B, cursor="hand2")
                editbtn.grid(row=0, column=1, sticky="ne", padx=(0,4), pady=8)
                editbtn.bind("<Button-1>", lambda e, mid=m["id"]: _edit(mid))
                editbtn.bind("<Enter>", lambda e, w=editbtn: w.config(fg=C["accent"]))
                editbtn.bind("<Leave>", lambda e, w=editbtn: w.config(fg=C["fg3"]))

                delbtn = tk.Label(row, text="\u2715", bg=C["input"], fg=C["fg3"],
                                  font=FONT_B, cursor="hand2")
                delbtn.grid(row=0, column=2, sticky="ne", padx=(0,8), pady=8)
                delbtn.bind("<Button-1>", lambda e, mid=m["id"]: (_del(mid)))
                delbtn.bind("<Enter>", lambda e, w=delbtn: w.config(fg=C["red"]))
                delbtn.bind("<Leave>", lambda e, w=delbtn: w.config(fg=C["fg3"]))

                grid_state["cards"].append(row)
            _regrid()

        def _del(mid):
            p["messages"] = [x for x in p["messages"] if x["id"] != mid]
            for lst in p.get("routing_disabled", {}).values():
                if mid in lst:
                    lst.remove(mid)
            save_data(self._data)
            if editing_message_id[0] == mid:
                _set_editing(None)
            _render_rows()
            self._routing_refreshers.get(pid, lambda: None)()

        scroll.canvas.bind("<Configure>", _regrid, add="+")
        _render_rows()

    def _section_routing(self, parent, p, pid):
        self._section_title(parent, "\U0001f3af", "Message Routing")
        c = card(parent)
        c.pack(fill="x", pady=(0, 12))

        tk.Label(c, text="Choose which messages get sent to which channels. Unchecked = skipped for that channel.",
                 bg=C["card"], fg=C["fg3"], font=FONT_SM, justify="left",
                 wraplength=640).pack(anchor="w", padx=10, pady=(8,4))

        wrap = tk.Frame(c, bg=C["card"])
        wrap.pack(fill="x", padx=8, pady=(0,8))
        scroll = BoundedScrollFrame(wrap, height=260, bg=C["card"])
        scroll.pack(fill="x")

        def _toggle(mid, cid, var):
            disabled = p.setdefault("routing_disabled", {})
            lst = disabled.setdefault(cid, [])
            if var.get():
                if mid in lst:
                    lst.remove(mid)
            else:
                if mid not in lst:
                    lst.append(mid)
            save_data(self._data)

        def _render():
            for w in scroll.inner.winfo_children():
                w.destroy()
            if not p["messages"] or not p["channels"]:
                tk.Label(scroll.inner, text="Add at least one message and one channel to configure routing.",
                         bg=C["card"], fg=C["fg3"], font=FONT_SM).pack(anchor="w", padx=6, pady=10)
                return
            disabled_map = p.get("routing_disabled", {})
            for i in range(3):
                scroll.inner.columnconfigure(i, weight=1)
            for idx, m in enumerate(p["messages"]):
                row = idx // 3
                col = idx % 3
                block = tk.Frame(scroll.inner, bg=C["input"],
                                 highlightthickness=1, highlightbackground=C["border"])
                block.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
                header = tk.Frame(block, bg=C["input"])
                header.pack(fill="x", padx=8, pady=(8,4))
                for ch in p["channels"]:
                    disabled_ids = disabled_map.get(ch["id"], [])
                    var = tk.BooleanVar(value=m["id"] not in disabled_ids)
                    cb = tk.Checkbutton(header, text=ch["label"], variable=var,
                                        bg=C["input"], fg=C["fg2"], selectcolor=C["card"],
                                        activebackground=C["input"], activeforeground=C["fg"],
                                        highlightthickness=0, bd=0, font=FONT_SM,
                                        command=lambda mid=m["id"], cid=ch["id"], v=var: _toggle(mid, cid, v))
                    cb.pack(side="left", padx=(0,6), pady=2)
                preview = m["content"] if len(m["content"]) <= 70 else m["content"][:67] + "..."
                tk.Label(block, text=preview, bg=C["input"], fg=C["fg"], font=MONO,
                         justify="left", anchor="w", wraplength=240).pack(fill="x", padx=8, pady=(0,10))

        _render()
        self._routing_refreshers[pid] = _render

    def _section_action(self, parent, p, pid):
        wrap = tk.Frame(parent, bg=C["panel"])
        wrap.pack(fill="x", pady=(18,6))
        running = pid in self._stop_events and not self._stop_events[pid].is_set()
        base = C["danger"] if running else C["accent"]
        self._toggle_btn = tk.Button(
            wrap,
            text="\u25a0  Stop Autopost" if running else "\u25b6  Start Autopost",
            command=lambda: self._toggle(pid),
            bg=base, fg=C["fg"], activebackground=base,
            activeforeground=C["fg"], relief="flat", font=FONT_XL,
            padx=14, pady=12, cursor="hand2", bd=0,
        )
        self._toggle_btn.base_bg = base
        self._toggle_btn.bind("<Enter>",
            lambda e: self._toggle_btn.config(bg=_darken(self._toggle_btn.base_bg)))
        self._toggle_btn.bind("<Leave>",
            lambda e: self._toggle_btn.config(bg=self._toggle_btn.base_bg))
        self._toggle_btn.pack(fill="x")

    def _toggle(self, pid):
        running = pid in self._stop_events and not self._stop_events[pid].is_set()
        if running:
            self._stop(pid)
        else:
            self._start(pid)

    def _section_status(self, parent, p, pid):
        self._section_title(parent, "\U0001f4ca", "Status")
        row = tk.Frame(parent, bg=C["panel"])
        row.pack(fill="x")
        row.columnconfigure((0,1,2), weight=1, uniform="stat")

        running = pid in self._stop_events and not self._stop_events[pid].is_set()
        uptime_secs = time.time() - self._start_times.get(pid, time.time()) if running else 0

        def stat_card(col, label_text):
            cc = card(row)
            cc.grid(row=0, column=col, sticky="nsew", padx=(0 if col==0 else 6, 0 if col==2 else 6))
            tk.Label(cc, text=label_text, bg=C["card"], fg=C["fg2"], font=FONT_SM).pack(pady=(10,2))
            val = tk.Label(cc, text="—", bg=C["card"], fg=C["fg"], font=FONT_XL)
            val.pack(pady=(0,10))
            return val

        status_val  = stat_card(0, "STATUS")
        sent_val    = stat_card(1, "TOTAL SENT")
        uptime_val  = stat_card(2, "UPTIME")

        status_val.config(text="Running" if running else "Idle",
                           fg=C["green"] if running else C["fg2"])
        sent_val.config(text=str(p.get("total_sent", 0)))
        uptime_val.config(text=_format_uptime(uptime_secs))

        countdown_wrap = card(parent)
        countdown_wrap.pack(fill="x", pady=(10,0))
        tk.Label(countdown_wrap, text="NEXT POST IN", bg=C["card"], fg=C["fg2"],
                 font=FONT_SM).pack(pady=(12,2))
        cd_label = tk.Label(countdown_wrap, text="—", bg=C["card"], fg=C["accent"],
                            font=("Consolas", 20, "bold"), justify="left", anchor="w")
        cd_label.pack(fill="x", padx=10, pady=(0,12))

        self._stat_widgets[pid] = {
            "status": status_val, "sent": sent_val, "uptime": uptime_val, "countdown": cd_label,
        }
        if running:
            cd_label.config(text=self._format_channel_next_send_text(p, pid))

    def _log(self, pid, text, tag="inf"):
        ts = time.strftime("%H:%M:%S")

        buf = self._log_buffers.setdefault(pid, [])
        buf.append((ts, text, tag))
        if len(buf) > 1000:
            del buf[: len(buf) - 1000]

        def _write():
            w = self._log_widgets.get(pid)
            if not w:
                return
            try:
                w.config(state="normal")
                w.insert("end", f"[{ts}] {text}\n", tag)
                w.see("end")
                w.config(state="disabled")
            except tk.TclError:
                self._log_widgets.pop(pid, None)
        self.after(0, _write)

    def _set_running_ui(self, pid, running):
        def _update():
            lbl = self._status_labels.get(pid)
            if lbl:
                try:
                    if running:
                        lbl.config(text="\u25cf Running", fg=C["green"])
                    else:
                        lbl.config(text="\u25cf Stopped", fg=C["red"])
                except tk.TclError:
                    self._status_labels.pop(pid, None)
            if self._active_id == pid and self._toggle_btn:
                try:
                    if running:
                        self._toggle_btn.config(text="\u25a0  Stop Autopost",
                                                 bg=C["danger"], activebackground=C["danger"])
                        self._toggle_btn.base_bg = C["danger"]
                    else:
                        self._toggle_btn.config(text="\u25b6  Start Autopost",
                                                 bg=C["accent"], activebackground=C["accent"])
                        self._toggle_btn.base_bg = C["accent"]
                except tk.TclError:
                    self._toggle_btn = None
            stats = self._stat_widgets.get(pid)
            if stats:
                try:
                    stats["status"].config(text="Running" if running else "Idle",
                                            fg=C["green"] if running else C["fg2"])
                    if not running:
                        stats["countdown"].config(text="\u2014")
                except tk.TclError:
                    self._stat_widgets.pop(pid, None)
            if not running:
                self._next_send_at.pop(pid, None)
                self._channel_next_send_at.pop(pid, None)
            self._refresh_sidebar()
        self.after(0, _update)

    def _refresh_stat_widgets(self, pid):
        def _update():
            stats = self._stat_widgets.get(pid)
            if not stats:
                return
            p = self._get_profile(pid)
            if not p:
                return
            try:
                stats["sent"].config(text=str(p.get("total_sent", 0)))
                uptime_secs = time.time() - self._start_times.get(pid, time.time())
                stats["uptime"].config(text=_format_uptime(uptime_secs))
                stats["countdown"].config(text=self._format_channel_next_send_text(p, pid))
            except tk.TclError:
                self._stat_widgets.pop(pid, None)
        self.after(0, _update)

    def _tick_active_profile_ui(self):
        pid = self._active_id
        if pid:
            stats = self._stat_widgets.get(pid)
            running = pid in self._stop_events and not self._stop_events[pid].is_set()
            if stats:
                try:
                    if running:
                        uptime_secs = time.time() - self._start_times.get(pid, time.time())
                        stats["uptime"].config(text=_format_uptime(uptime_secs))
                        stats["countdown"].config(text=self._format_channel_next_send_text(self._get_profile(pid), pid))
                    else:
                        stats["countdown"].config(text="—")
                except tk.TclError:
                    self._stat_widgets.pop(pid, None)
        self.after(1000, self._tick_active_profile_ui)

    def _start(self, pid):
        p = self._get_profile(pid)
        if not p: return
        if not p["accounts"]:
            messagebox.showwarning("No accounts", "Add at least one account.")
            return
        if not p["channels"]:
            messagebox.showwarning("No channels", "Add at least one channel.")
            return
        if not p["messages"]:
            messagebox.showwarning("No messages", "Add at least one message.")
            return
        routing_disabled = p.get("routing_disabled", {})
        if all(len(routing_disabled.get(ch["id"], [])) >= len(p["messages"]) for ch in p["channels"]):
            messagebox.showwarning(
                "Nothing to send",
                "Every channel has all messages unchecked in Message Routing.\n"
                "Enable at least one message per channel before starting.")
            return
        ev = threading.Event()
        self._stop_events[pid] = ev
        t = threading.Thread(target=self._run_profile, args=(pid, p, ev), daemon=True)
        self._threads[pid] = t
        initial_map = {
            ch["id"]: time.time() + float(ch.get("delay", 5))
            for ch in p.get("channels", [])
        }
        self._set_channel_next_send(pid, initial_map)
        self._set_running_ui(pid, True)
        t.start()

    def _stop(self, pid):
        if pid in self._stop_events:
            self._stop_events[pid].set()
        self._log(pid, "Stop requested…", "inf")

    def _run_profile(self, pid, p, stop_ev):
        profile_label = p.get("label", "Profile")
        self._start_times[pid] = time.time()
        total_sent = p.setdefault("total_sent", 0)
        start_msg = f"Starting — {len(p['accounts'])} account(s), {len(p['channels'])} channel(s), {len(p['messages'])} message(s)"
        self._log(pid, start_msg, "inf")

        channel_next_at = {
            ch['id']: time.time() + float(ch.get('delay', 5))
            for ch in p.get('channels', [])
        }
        self._set_channel_next_send(pid, channel_next_at)

        valid_msg_ids = {msg['id'] for msg in p.get('messages', [])}
        routing_disabled = p.setdefault("routing_disabled", {})
        normalized_disabled = {}
        for ch in p.get('channels', []):
            ids = routing_disabled.get(ch['id'], [])
            normalized_disabled[ch['id']] = [mid for mid in ids if mid in valid_msg_ids]
        if normalized_disabled != routing_disabled:
            p['routing_disabled'] = normalized_disabled
            save_data(self._data)

        try:
            while not stop_ev.is_set():
                routing_disabled = p.get("routing_disabled", {})
                for msg in p["messages"]:
                    if stop_ev.is_set(): break
                    allowed_channels = [ch for ch in p["channels"]
                                        if msg["id"] not in set(routing_disabled.get(ch["id"], []))]
                    if not allowed_channels:
                        self._log(pid, f"Skipping message for all channels — message not selected anywhere.", "inf")
                        continue
                    pending_channels = list(allowed_channels)
                    while pending_channels and not stop_ev.is_set():
                        now = time.time()
                        ready_channels = [ch for ch in pending_channels
                                          if channel_next_at.get(ch["id"], 0) <= now]
                        if not ready_channels:
                            next_at = min(channel_next_at.get(ch["id"], now)
                                          for ch in pending_channels)
                            self._set_channel_next_send(pid, channel_next_at)
                            self._refresh_stat_widgets(pid)
                            stop_ev.wait(max(0, next_at - now))
                            continue
                        for ch in ready_channels:
                            if stop_ev.is_set(): break
                            channel_delay = float(ch.get("delay", 5))
                            for acc in p["accounts"]:
                                if stop_ev.is_set(): break
                                ok, reason = send_message(acc["token"], ch["channel_id"], msg["content"], acc["type"] == "bot")
                                total_sent += 1
                                p["total_sent"] = total_sent
                                save_data(self._data)
                                if ok:
                                    self._log(pid, f"\u2705 [{acc['label']}] \u2192 #{ch['label']}", "ok")
                                else:
                                    self._log(pid, f"\u274c [{acc['label']}] \u2192 #{ch['label']}: {reason}", "err")
                            channel_next_at[ch["id"]] = time.time() + channel_delay
                            self._set_channel_next_send(pid, channel_next_at)
                            self._refresh_stat_widgets(pid)
                            pending_channels = [x for x in pending_channels if x["id"] != ch["id"]]
                        if pending_channels and not stop_ev.is_set():
                            continue
                if stop_ev.is_set(): break
                self._log(pid, "─── Round complete ───", "sep")
                stop_ev.wait(0.5)
        finally:
            if pid in self._stop_events:
                del self._stop_events[pid]
            self._start_times.pop(pid, None)
            self._next_send_at.pop(pid, None)
            self._channel_next_send_at.pop(pid, None)
            self._set_running_ui(pid, False)
            self._log(pid, "Stopped.", "inf")

    def _on_close(self):
        for ev in self._stop_events.values():
            ev.set()
        self.destroy()

if __name__ == "__main__":
    try:
        App().mainloop()
    except Exception as exc:
        try:
            import tkinter.messagebox as _mb
            _mb.showerror("Startup Error", str(exc))
        except Exception:
            print(f"Fatal: {exc}", file=sys.stderr)
        sys.exit(1)
