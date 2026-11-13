"""Mouse and keyboard accessible help for Tk widgets."""

import tkinter as tk
from tkinter import ttk


class Tooltip:
    def __init__(self, widget, text, delay=550):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.pending = None
        self.window = None
        for event in ('<Enter>', '<FocusIn>'):
            widget.bind(event, self.schedule, add='+')
        for event in ('<Leave>', '<FocusOut>', '<ButtonPress>', '<KeyPress>', '<Destroy>'):
            widget.bind(event, self.hide, add='+')

    def schedule(self, _event=None):
        self.hide()
        self.pending = self.widget.after(self.delay, self.show)

    def show(self):
        self.pending = None
        if not self.widget.winfo_exists() or not self.widget.winfo_viewable():
            return
        self.window = window = tk.Toplevel(self.widget)
        window.withdraw()
        window.overrideredirect(True)
        window.attributes('-topmost', True)
        text = self.text() if callable(self.text) else self.text
        ttk.Label(window, text=text, padding=10, justify=tk.LEFT,
                  wraplength=430, relief=tk.SOLID, borderwidth=1).pack()
        window.update_idletasks()
        x = min(self.widget.winfo_rootx() + 12,
                window.winfo_screenwidth() - window.winfo_reqwidth() - 8)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        if y + window.winfo_reqheight() > window.winfo_screenheight():
            y = self.widget.winfo_rooty() - window.winfo_reqheight() - 6
        window.geometry(f'+{max(0, x)}+{max(0, y)}')
        window.deiconify()

    def hide(self, _event=None):
        if self.pending is not None:
            self.widget.after_cancel(self.pending)
            self.pending = None
        if self.window is not None:
            self.window.destroy()
            self.window = None
