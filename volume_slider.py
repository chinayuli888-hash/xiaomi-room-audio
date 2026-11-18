"""A compact, keyboard-operable volume slider for the Windows panel."""
import math
import tkinter as tk


class VolumeSlider(tk.Canvas):
    """Keep the shared DoubleVar and ttk-style disabled state used by the panel."""
    def __init__(self, parent, *, variable, command, width=124, height=30,
                 background="#18242E", accent="#5BE2A8"):
        super().__init__(parent, width=width, height=height, background=background,
                         highlightthickness=0, borderwidth=0, takefocus=True,
                         cursor="hand2")
        self.variable, self.command = variable, command
        self.accent = accent
        self.disabled = False
        self._trace = variable.trace_add("write", self._draw)
        self.bind("<Configure>", self._draw)
        self.bind("<FocusIn>", self._draw)
        self.bind("<FocusOut>", self._draw)
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<KeyPress>", self._key)
        self.bind("<Destroy>", self._destroy)

    def state(self, states=None):
        if states is None:
            return ("disabled",) if self.disabled else ()
        previous = self.disabled
        for flag in states:
            if flag in ("disabled", "!disabled"):
                self.disabled = flag == "disabled"
        self.configure(cursor="arrow" if self.disabled else "hand2",
                       takefocus=not self.disabled)
        self._draw()
        return (("disabled",) if previous else ("!disabled",)) if previous != self.disabled else ()

    def get(self):
        try:
            value = float(self.variable.get())
        except (ValueError, tk.TclError):
            return 0.0
        return min(100.0, max(0.0, value)) if math.isfinite(value) else 0.0

    def _track(self):
        margin = 10
        return margin, max(margin + 1, self.winfo_width() - margin)

    def _draw(self, *_):
        if not self.winfo_exists():
            return
        self.delete("all")
        left, right = self._track()
        y = self.winfo_height() / 2
        x = left + (right - left) * self.get() / 100
        color = "#62788A" if self.disabled else self.accent
        self.create_line(left, y, right, y, fill="#344D5B", width=4, capstyle=tk.ROUND)
        if x > left:
            self.create_line(left, y, x, y, fill=color, width=4, capstyle=tk.ROUND)
        if not self.disabled and self.focus_get() is self:
            self.create_oval(x - 9, y - 9, x + 9, y + 9,
                             outline=self.accent, width=1)
        self.create_oval(x - 6, y - 6, x + 6, y + 6, fill=color, outline="")

    def _set_from_user(self, amount):
        if self.disabled:
            return
        amount = round(min(100.0, max(0.0, amount)))
        if amount != self.get():
            self.variable.set(amount)
            self.command(str(amount))

    def _press(self, event):
        if not self.disabled:
            self.focus_set()
            self._drag(event)
        return "break"

    def _drag(self, event):
        left, right = self._track()
        self._set_from_user(100 * (event.x - left) / (right - left))
        return "break"

    def _key(self, event):
        if self.disabled:
            return None
        steps = {"Left": -1, "Down": -1, "Right": 1, "Up": 1,
                 "Prior": 10, "Next": -10}
        if event.keysym in steps:
            self._set_from_user(self.get() + steps[event.keysym])
        elif event.keysym == "Home":
            self._set_from_user(0)
        elif event.keysym == "End":
            self._set_from_user(100)
        else:
            return None
        return "break"

    def _destroy(self, event):
        if event.widget is self:
            self.variable.trace_remove("write", self._trace)
