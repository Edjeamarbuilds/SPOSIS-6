import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import filedialog, messagebox, simpledialog, ttk

DB_FILE = "salon_pos.db"
LEGACY_FILE = "salon_data.json"
DEFAULT_ADMIN = "administrator"
DEFAULT_PASSWORD = "admin 123"


def money(value):
    return "₱{:,.2f}".format(float(value or 0))


def make_password(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 180000)
    return salt.hex() + "$" + digest.hex()


def check_password(password, saved):
    try:
        salt_hex, digest_hex = saved.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), 180000)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


class SalonDatabase:
    def __init__(self, path=DB_FILE):
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.create_schema()
        self.create_first_admin()
        self.import_legacy_data()

    def create_schema(self):
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('Admin', 'Cashier')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                recovery_code_hash TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price REAL NOT NULL CHECK(price >= 0),
                stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
                low_stock_level INTEGER NOT NULL DEFAULT 5 CHECK(low_stock_level >= 0),
                description TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price REAL NOT NULL CHECK(price >= 0)
            );
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sold_at TEXT NOT NULL,
                total REAL NOT NULL CHECK(total >= 0),
                user_id INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS sale_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id INTEGER NOT NULL,
                item_type TEXT NOT NULL CHECK(item_type IN ('product', 'service')),
                item_id INTEGER,
                item_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                unit_price REAL NOT NULL CHECK(unit_price >= 0),
                FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(users)")}
        if "email" not in columns:
            self.connection.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        if "recovery_code_hash" not in columns:
            self.connection.execute("ALTER TABLE users ADD COLUMN recovery_code_hash TEXT NOT NULL DEFAULT ''")
        self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique "
                                "ON users(email COLLATE NOCASE) WHERE email <> ''")
        self.connection.commit()

    def create_first_admin(self):
        count = self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if not count:
            self.connection.execute(
                "INSERT INTO users(username,password_hash,role,active,created_at) VALUES(?,?,?,?,?)",
                (DEFAULT_ADMIN, make_password(DEFAULT_PASSWORD), "Admin", 1,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self.connection.commit()

    def import_legacy_data(self):
        imported = self.connection.execute(
            "SELECT value FROM settings WHERE key='legacy_json_imported'").fetchone()
        if imported or not os.path.exists(LEGACY_FILE):
            return
        try:
            with open(LEGACY_FILE, "r") as source:
                data = json.load(source)
        except (IOError, ValueError):
            return
        cursor = self.connection.cursor()
        for item in data.get("products", []):
            cursor.execute("INSERT INTO products(id,name,price,stock) VALUES(?,?,?,?)",
                           (item.get("id"), item.get("name", "Product"),
                            max(0, float(item.get("price", 0))),
                            max(0, int(item.get("stock", 0)))))
        for item in data.get("services", []):
            cursor.execute("INSERT INTO services(id,name,price) VALUES(?,?,?)",
                           (item.get("id"), item.get("name", "Service"),
                            max(0, float(item.get("price", 0)))))
        for sale in data.get("sales", []):
            cursor.execute("INSERT INTO sales(id,sold_at,total) VALUES(?,?,?)",
                           (sale.get("id"), sale.get("date", datetime.now().isoformat()),
                            max(0, float(sale.get("total", 0)))))
            sale_id = cursor.lastrowid
            for line in sale.get("items", []):
                cursor.execute("""INSERT INTO sale_items
                    (sale_id,item_type,item_id,item_name,quantity,unit_price)
                    VALUES(?,?,?,?,?,?)""",
                    (sale_id, line.get("type", "service"), line.get("product_id"),
                     line.get("name", "Item"), max(1, int(line.get("quantity", 1))),
                     max(0, float(line.get("unit_price", 0)))))
        cursor.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('legacy_json_imported','1')")
        self.connection.commit()

    def get_setting(self, key, default=""):
        row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.connection.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                                (key, value))
        self.connection.commit()


class SalonApp(tk.Tk):
    def __init__(self):
        tk.Tk.__init__(self)
        self.title("CB SPOSIS | Salon POS & Inventory")
        self.geometry("1180x760")
        self.minsize(900, 600)
        self.configure(bg="#f6f3f6")
        self.db = SalonDatabase()
        self.user = None
        self.cart = []
        self.colors = {"nav": "#302638", "accent": "#a45d78", "bg": "#f6f3f6",
                       "ink": "#302738", "muted": "#817786", "white": "#ffffff"}
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.style.configure("Treeview", rowheight=30, font=("Arial", 10),
                             background="white", fieldbackground="white")
        self.style.configure("Treeview.Heading", font=("Arial", 10, "bold"),
                             background="#f1eaef", foreground=self.colors["ink"])
        self.style.map("Treeview", background=[("selected", "#ead9e1")],
                       foreground=[("selected", self.colors["ink"])])
        self.show_login()

    def clear(self):
        for child in self.winfo_children():
            child.destroy()

    def show_login(self):
        self.user = None
        self.password_visible = False
        self.clear()
        outer = tk.Frame(self, bg=self.colors["bg"])
        outer.pack(fill="both", expand=True)
        card = tk.Frame(outer, bg="white", padx=34, pady=28,
                        highlightbackground="#e8dce3", highlightthickness=1)
        card.place(relx=.5, rely=.5, anchor="center", width=430)

        logo = tk.Canvas(card, width=54, height=54, bg="white", highlightthickness=0)
        logo.pack(anchor="w", pady=(0, 12))
        logo.create_oval(3, 3, 51, 51, fill=self.colors["nav"], outline="")
        logo.create_text(27, 27, text="✦", fill="#f0c9d8", font=("Arial", 25, "bold"))
        tk.Label(card, text="CB SPOSIS", bg="white", fg=self.colors["ink"],
                 font=("Arial", 23, "bold")).pack(anchor="w")
        tk.Label(card, text="Salon POS & Inventory", bg="white", fg=self.colors["muted"],
                 font=("Arial", 10)).pack(anchor="w", pady=(2, 20))

        tk.Label(card, text="Welcome back", bg="white", fg=self.colors["ink"],
                 font=("Arial", 15, "bold")).pack(anchor="w", pady=(0, 10))
        tk.Label(card, text="Username", bg="white", fg=self.colors["ink"],
                 font=("Arial", 10, "bold")).pack(anchor="w", pady=(3, 5))
        self.login_user = tk.Entry(card, relief="solid", bd=1, font=("Arial", 11))
        self.login_user.pack(fill="x", ipady=8)
        self.add_placeholder(self.login_user, "Enter your username")

        tk.Label(card, text="Password", bg="white", fg=self.colors["ink"],
                 font=("Arial", 10, "bold")).pack(anchor="w", pady=(14, 5))
        password_row = tk.Frame(card, bg="white")
        password_row.pack(fill="x")
        self.login_pass = tk.Entry(password_row, relief="solid", bd=1, font=("Arial", 11), show="•")
        self.login_pass.pack(side="left", fill="x", expand=True, ipady=8)
        self.add_placeholder(self.login_pass, "Enter your password")
        self.password_toggle = tk.Button(password_row, text="Show", command=self.toggle_password,
                                         bg="white", fg=self.colors["accent"], relief="flat",
                                         font=("Arial", 9, "bold"), cursor="hand2")
        self.password_toggle.pack(side="right", padx=(8, 0))

        options = tk.Frame(card, bg="white")
        options.pack(fill="x", pady=(13, 4))
        remembered = self.db.get_setting("remembered_username", "")
        self.remember_var = tk.IntVar(value=1 if remembered else 0)
        tk.Checkbutton(options, text="Remember me", variable=self.remember_var,
                       bg="white", fg=self.colors["muted"], activebackground="white",
                       selectcolor="white", font=("Arial", 9)).pack(side="left")
        tk.Button(options, text="Forgot password?", command=self.forgot_password,
                  bg="white", fg=self.colors["accent"], activebackground="white",
                  relief="flat", font=("Arial", 9, "bold"), cursor="hand2").pack(side="right")

        self.login_error = tk.Label(card, text="", bg="white", fg="#b74450",
                                    font=("Arial", 9), anchor="w", wraplength=350)
        self.login_error.pack(fill="x", pady=(2, 6))
        tk.Button(card, text="Log in", command=self.login, bg=self.colors["accent"],
                  fg="white", activebackground="#85445f", activeforeground="white",
                  relief="flat", font=("Arial", 11, "bold"), padx=12, pady=10,
                  cursor="hand2").pack(fill="x", pady=(2, 0))

        if remembered:
            self.login_user.delete(0, "end")
            self.login_user.insert(0, remembered)
            self.login_user.config(fg=self.colors["ink"])
            self.login_user.placeholder_active = False
            self.login_pass.focus_set()
        else:
            self.login_user.focus_set()
        self.login_pass.bind("<Return>", lambda event: self.login())
        self.login_user.bind("<Return>", lambda event: self.login_pass.focus_set())

    def add_placeholder(self, entry, text):
        entry.placeholder_text = text
        entry.placeholder_active = True
        entry.insert(0, text)
        entry.config(fg=self.colors["muted"])

        def clear_placeholder(event):
            if entry.placeholder_active:
                entry.delete(0, "end")
                entry.placeholder_active = False
                entry.config(fg=self.colors["ink"])
                if entry is getattr(self, "login_pass", None):
                    entry.config(show="" if self.password_visible else "•")

        def restore_placeholder(event):
            if not entry.get():
                entry.placeholder_active = True
                entry.insert(0, text)
                entry.config(fg=self.colors["muted"])
                if entry is getattr(self, "login_pass", None):
                    entry.config(show="")

        entry.bind("<FocusIn>", clear_placeholder)
        entry.bind("<FocusOut>", restore_placeholder)

    def toggle_password(self):
        self.password_visible = not self.password_visible
        if self.login_pass.placeholder_active:
            self.login_pass.config(show="")
        else:
            self.login_pass.config(show="" if self.password_visible else "•")
        self.password_toggle.config(text="Hide" if self.password_visible else "Show")

    def forgot_password(self):
        self.show_password_recovery()

    def show_password_recovery(self):
        self.clear()
        outer = tk.Frame(self, bg=self.colors["bg"])
        outer.pack(fill="both", expand=True)
        card = tk.Frame(outer, bg="white", padx=34, pady=28,
                        highlightbackground="#e8dce3", highlightthickness=1)
        card.place(relx=.5, rely=.5, anchor="center", width=500)

        tk.Label(card, text="CB SPOSIS", bg="white", fg=self.colors["ink"],
                 font=("Arial", 22, "bold")).pack(anchor="w")
        tk.Label(card, text="Recover your account", bg="white", fg=self.colors["ink"],
                 font=("Arial", 15, "bold")).pack(anchor="w", pady=(18, 4))
        tk.Label(card, text="Enter your username or registered email and the recovery code "
                 "provided by your administrator.", bg="white", fg=self.colors["muted"],
                 wraplength=410, justify="left").pack(anchor="w", pady=(0, 12))

        identifier = self.field(card, "Username or registered email")
        recovery_code = self.field(card, "One-time recovery code", show="•")
        error = tk.Label(card, text="", bg="white", fg="#b74450", anchor="w",
                         wraplength=410, justify="left")
        error.pack(fill="x", pady=(8, 4))
        password_fields = tk.Frame(card, bg="white")

        def verify_account():
            key = identifier.get().strip()
            code = recovery_code.get().strip()
            if not key or not code:
                error.config(text="Enter your username or email and recovery code.")
                return
            row = self.db.connection.execute(
                "SELECT * FROM users WHERE active=1 AND "
                "(username=? COLLATE NOCASE OR email=? COLLATE NOCASE)",
                (key, key)).fetchone()
            if not row or not row["recovery_code_hash"] or not check_password(code, row["recovery_code_hash"]):
                error.config(text="Account or recovery code could not be verified. Contact your administrator for help.")
                return
            identifier.config(state="disabled")
            recovery_code.config(state="disabled")
            verify_button.pack_forget()
            tk.Label(password_fields, text="Create a new password (at least 8 characters)",
                     bg="white", fg=self.colors["ink"], font=("Arial", 10, "bold")).pack(anchor="w", pady=(8, 5))
            new_password = tk.Entry(password_fields, show="•", relief="solid", bd=1, font=("Arial", 11))
            new_password.pack(fill="x", ipady=7)
            tk.Label(password_fields, text="Confirm new password", bg="white", fg=self.colors["ink"],
                     font=("Arial", 10, "bold")).pack(anchor="w", pady=(10, 5))
            confirm_password = tk.Entry(password_fields, show="•", relief="solid", bd=1, font=("Arial", 11))
            confirm_password.pack(fill="x", ipady=7)
            password_fields.pack(fill="x", pady=(0, 8))

            def reset_password():
                new = new_password.get()
                confirmation = confirm_password.get()
                if len(new) < 8:
                    error.config(text="Use a password with at least 8 characters.")
                    return
                if new != confirmation:
                    error.config(text="The passwords do not match.")
                    return
                try:
                    changed = self.db.connection.execute(
                        "UPDATE users SET password_hash=?, recovery_code_hash='' "
                        "WHERE id=? AND recovery_code_hash=? AND active=1",
                        (make_password(new), row["id"], row["recovery_code_hash"])).rowcount
                    self.db.connection.commit()
                except sqlite3.Error:
                    self.db.connection.rollback()
                    error.config(text="Could not update the password. Please try again.")
                    return
                if not changed:
                    error.config(text="This recovery code has expired. Request a new code from your administrator.")
                    return
                messagebox.showinfo("Password updated", "Your password has been changed. Please log in with your new password.", parent=self)
                self.show_login()

            self.button(password_fields, "Reset password", reset_password).pack(anchor="e", pady=(12, 0))
            error.config(text="Identity verified. Choose a new password below.", fg=self.colors["accent"])
            new_password.focus_set()

        verify_button = self.button(card, "Verify identity", verify_account)
        verify_button.pack(fill="x", pady=(4, 0))
        self.button(card, "← Back to Login", self.show_login, "secondary").pack(anchor="w", pady=(14, 0))
        identifier.focus_set()
        recovery_code.bind("<Return>", lambda event: verify_account())

    def field(self, parent, label, show=None, value=""):
        tk.Label(parent, text=label, bg=parent.cget("bg"), fg=self.colors["ink"],
                 font=("Arial", 10, "bold")).pack(anchor="w", pady=(10, 5))
        entry = tk.Entry(parent, show=show, relief="solid", bd=1, font=("Arial", 11))
        entry.pack(fill="x", ipady=8)
        if value:
            entry.insert(0, value)
        return entry

    def login(self):
        username = "" if self.login_user.placeholder_active else self.login_user.get().strip()
        password = "" if self.login_pass.placeholder_active else self.login_pass.get()
        row = self.db.connection.execute(
            "SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (username,)).fetchone()
        if not row or not check_password(password, row["password_hash"]):
            self.login_error.config(text="Incorrect username or password, or this account is inactive.")
            return
        if self.remember_var.get():
            self.db.set_setting("remembered_username", username)
        else:
            self.db.set_setting("remembered_username", "")
        self.user = row
        self.show_app()

    def show_app(self):
        self.clear()
        shell = tk.Frame(self, bg=self.colors["bg"])
        shell.pack(fill="both", expand=True)
        sidebar = tk.Frame(shell, bg=self.colors["nav"], width=215)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="CB SPOSIS", bg=self.colors["nav"], fg="white",
                 font=("Arial", 19, "bold")).pack(anchor="w", padx=20, pady=(24, 2))
        tk.Label(sidebar, text="SALON MANAGEMENT", bg=self.colors["nav"], fg="#b9abbc",
                 font=("Arial", 8, "bold")).pack(anchor="w", padx=21, pady=(0, 22))
        pages = [("Dashboard", "dashboard"), ("New Sale", "pos"), ("Products", "products"),
                 ("Services", "services"), ("Transactions", "sales"), ("Reports", "reports"),
                 ("Users", "users"), ("Settings", "settings")]
        if self.user["role"] != "Admin":
            pages = [item for item in pages if item[1] == "pos"]
        for label, key in pages:
            tk.Button(sidebar, text="  " + label, anchor="w", command=lambda p=key: self.open_page(p),
                      bg=self.colors["nav"], fg="#eee8ef", activebackground="#514456",
                      activeforeground="white", relief="flat", font=("Arial", 10),
                      padx=18, pady=11).pack(fill="x", padx=8, pady=2)
        tk.Label(sidebar, text="Signed in as\n{} · {}".format(self.user["username"], self.user["role"]),
                 bg=self.colors["nav"], fg="#c8bdcb", justify="left",
                 font=("Arial", 9)).pack(side="bottom", anchor="w", padx=20, pady=(0, 8))
        tk.Button(sidebar, text="Log out", command=self.show_login, anchor="w",
                  bg="#403647", fg="white", relief="flat", padx=18, pady=10).pack(side="bottom", fill="x", padx=8, pady=12)
        self.content = tk.Frame(shell, bg=self.colors["bg"])
        self.content.pack(side="left", fill="both", expand=True)
        self.open_page("dashboard" if self.user["role"] == "Admin" else "pos")

    def require_admin(self):
        if self.user and self.user["role"] == "Admin":
            return True
        messagebox.showerror("Permission denied", "Administrator access is required.")
        return False

    def heading(self, title, subtitle="Manage your salon workspace"):
        header = tk.Frame(self.content, bg=self.colors["bg"])
        header.pack(fill="x", padx=28, pady=(23, 15))
        tk.Label(header, text=title, bg=self.colors["bg"], fg=self.colors["ink"],
                 font=("Arial", 23, "bold")).pack(anchor="w")
        tk.Label(header, text=subtitle, bg=self.colors["bg"], fg=self.colors["muted"],
                 font=("Arial", 10)).pack(anchor="w", pady=(3, 0))

    def open_page(self, page):
        if page != "pos" and not self.require_admin():
            return
        for child in self.content.winfo_children():
            child.destroy()
        handlers = {"dashboard": self.page_dashboard, "products": self.page_products,
                    "services": self.page_services, "sales": self.page_sales,
                    "reports": self.page_reports, "users": self.page_users,
                    "settings": self.page_settings, "pos": self.page_pos}
        handlers[page]()

    def panel(self, parent=None, padx=18, pady=16):
        frame = tk.Frame(parent or self.content, bg="white", padx=padx, pady=pady,
                         highlightbackground="#e9e1e8", highlightthickness=1)
        return frame

    def tree(self, parent, columns, headings, widths=None):
        wrap = tk.Frame(parent, bg="white")
        wrap.pack(fill="both", expand=True)
        view = ttk.Treeview(wrap, columns=columns, show="headings")
        for index, column in enumerate(columns):
            view.heading(column, text=headings[index])
            view.column(column, width=(widths[index] if widths else 130), anchor="w")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=view.yview)
        view.configure(yscrollcommand=scroll.set)
        view.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return view

    def button(self, parent, text, command, kind="primary"):
        palette = {"primary": (self.colors["accent"], "white"),
                   "secondary": ("#f1e8ed", "#70465a"),
                   "danger": ("#fff0f0", "#b74450")}
        bg, fg = palette[kind]
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground=bg, activeforeground=fg, relief="flat",
                         padx=13, pady=8, font=("Arial", 9, "bold"), cursor="hand2")

    def confirm_action(self, title, message, confirm_text="Confirm", danger=False, parent=None):
        owner = parent or self
        dialog = tk.Toplevel(owner)
        dialog.title(title)
        dialog.transient(owner)
        dialog.resizable(False, False)
        dialog.configure(bg=self.colors["bg"])
        result = {"confirmed": False}

        card = tk.Frame(dialog, bg="white", padx=26, pady=22,
                        highlightbackground="#e9e1e8", highlightthickness=1)
        card.pack(fill="both", expand=True, padx=10, pady=10)
        accent = "#b74450" if danger else self.colors["accent"]
        icon = tk.Canvas(card, width=46, height=46, bg="white", highlightthickness=0)
        icon.pack(anchor="w", pady=(0, 12))
        icon.create_oval(2, 2, 44, 44, fill="#fff0f0" if danger else "#f5e9ed", outline="")
        icon.create_text(23, 23, text="!" if danger else "✓", fill=accent,
                         font=("Arial", 21, "bold"))
        tk.Label(card, text=title, bg="white", fg=self.colors["ink"],
                 font=("Arial", 16, "bold")).pack(anchor="w")
        tk.Label(card, text=message, bg="white", fg=self.colors["muted"],
                 font=("Arial", 10), justify="left", wraplength=340).pack(anchor="w", pady=(7, 20))

        actions = tk.Frame(card, bg="white")
        actions.pack(fill="x")
        def close(confirmed=False):
            result["confirmed"] = confirmed
            dialog.destroy()
        tk.Button(actions, text="Cancel", command=lambda: close(), bg="#f1e8ed",
                  fg="#70465a", activebackground="#e8dce3", relief="flat",
                  padx=14, pady=9, font=("Arial", 9, "bold"), cursor="hand2").pack(side="right", padx=(8, 0))
        tk.Button(actions, text=confirm_text, command=lambda: close(True), bg=accent,
                  fg="white", activebackground="#85445f" if not danger else "#963944",
                  activeforeground="white", relief="flat", padx=14, pady=9,
                  font=("Arial", 9, "bold"), cursor="hand2").pack(side="right")

        dialog.bind("<Escape>", lambda event: close())
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.update_idletasks()
        x = owner.winfo_rootx() + max(0, (owner.winfo_width() - dialog.winfo_width()) // 2)
        y = owner.winfo_rooty() + max(0, (owner.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry("+{}+{}".format(x, y))
        dialog.grab_set()
        owner.wait_window(dialog)
        return result["confirmed"]

    def page_dashboard(self):
        if not self.require_admin():
            return
        self.heading("Dashboard", "A quick overview of today's salon activity")
        body = tk.Frame(self.content, bg=self.colors["bg"])
        body.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        today = date.today().isoformat()
        total_sales = self.db.connection.execute("SELECT COALESCE(SUM(total),0) FROM sales").fetchone()[0]
        products = self.db.connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        services = self.db.connection.execute("SELECT COUNT(*) FROM services").fetchone()[0]
        low = self.db.connection.execute("SELECT COUNT(*) FROM products WHERE stock<=low_stock_level").fetchone()[0]
        users = self.db.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        cards = [("Total sales", money(total_sales)), ("Total products", str(products)),
                 ("Total services", str(services)), ("Low stock", str(low)), ("Total users", str(users))]
        grid = tk.Frame(body, bg=self.colors["bg"])
        grid.pack(fill="x")
        for index, pair in enumerate(cards):
            card = self.panel(grid, 16, 15)
            card.grid(row=index // 3, column=index % 3, sticky="nsew", padx=6, pady=6)
            tk.Label(card, text=pair[0], bg="white", fg=self.colors["muted"], font=("Arial", 10)).pack(anchor="w")
            tk.Label(card, text=pair[1], bg="white", fg=self.colors["ink"], font=("Arial", 21, "bold")).pack(anchor="w", pady=(9, 0))
        for col in range(3):
            grid.grid_columnconfigure(col, weight=1)
        panel = self.panel(body)
        panel.pack(fill="both", expand=True, padx=6, pady=(12, 0))
        tk.Label(panel, text="Recent transactions", bg="white", fg=self.colors["ink"],
                 font=("Arial", 13, "bold")).pack(anchor="w", pady=(0, 12))
        view = self.tree(panel, ("id", "date", "cashier", "total"),
                         ("Sale #", "Date", "User", "Total"), (75, 240, 160, 130))
        rows = self.db.connection.execute("""SELECT s.id,s.sold_at,u.username,s.total FROM sales s
            LEFT JOIN users u ON u.id=s.user_id ORDER BY s.id DESC LIMIT 8""").fetchall()
        for row in rows:
            view.insert("", "end", values=(row["id"], row["sold_at"], row["username"] or "Deleted user", money(row["total"])))
        tk.Label(panel, text="Today's sales: {}".format(money(self.db.connection.execute(
            "SELECT COALESCE(SUM(total),0) FROM sales WHERE date(sold_at)=?", (today,)).fetchone()[0])),
            bg="white", fg=self.colors["accent"], font=("Arial", 10, "bold")).pack(anchor="e", pady=(10, 0))

    def page_products(self):
        self.heading("Products", "Manage pricing, inventory, and low-stock levels")
        panel = self.panel(); panel.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        toolbar = tk.Frame(panel, bg="white"); toolbar.pack(fill="x", pady=(0, 12))
        search = tk.Entry(toolbar, width=28, relief="solid", bd=1)
        search.pack(side="left", ipady=6); search.insert(0, "Search products")
        view = self.tree(panel, ("id", "name", "price", "stock", "threshold", "description"),
                         ("ID", "Product", "Price", "Stock", "Low stock at", "Description"),
                         (55, 180, 100, 80, 105, 260))
        def refresh():
            view.delete(*view.get_children())
            term = search.get().strip()
            rows = self.db.connection.execute("SELECT * FROM products WHERE name LIKE ? ORDER BY name",
                                              ("%{}%".format(term if term != "Search products" else ""),)).fetchall()
            for row in rows:
                view.insert("", "end", iid=str(row["id"]), values=(row["id"], row["name"], money(row["price"]), row["stock"], row["low_stock_level"], row["description"]))
        def edit(selected=None):
            values = view.item(selected, "values") if selected else None
            self.product_dialog(values, refresh)
        self.button(toolbar, "Search", refresh, "secondary").pack(side="left", padx=8)
        self.button(toolbar, "+ Add product", lambda: edit(), "primary").pack(side="right", padx=3)
        self.button(toolbar, "Edit selected", lambda: edit(view.focus()) if view.focus() else messagebox.showinfo("Select product", "Choose a product first.")).pack(side="right", padx=3)
        self.button(toolbar, "Delete", lambda: self.delete_product(view, refresh), "danger").pack(side="right", padx=3)
        view.bind("<Double-1>", lambda event: edit(view.focus()) if view.focus() else None)
        refresh()

    def product_dialog(self, values, done):
        dialog = tk.Toplevel(self); dialog.title("Product details"); dialog.transient(self); dialog.grab_set(); dialog.resizable(False, False)
        box = tk.Frame(dialog, bg="white", padx=24, pady=20); box.pack(fill="both", expand=True)
        fields = {}
        defaults = {"name": values[1] if values else "", "price": values[2].replace("₱", "").replace(",", "") if values else "",
                    "stock": values[3] if values else "0", "threshold": values[4] if values else "5",
                    "description": values[5] if values else ""}
        for key, label in [("name", "Product name"), ("price", "Price (₱)"), ("stock", "Quantity in stock"), ("threshold", "Low-stock threshold"), ("description", "Description")]:
            tk.Label(box, text=label, bg="white", fg=self.colors["ink"], font=("Arial", 9, "bold")).pack(anchor="w", pady=(7, 3))
            entry = tk.Entry(box, width=42); entry.pack(fill="x", ipady=5); entry.insert(0, defaults[key]); fields[key] = entry
        def save():
            try:
                name = fields["name"].get().strip(); price = float(fields["price"].get())
                stock = int(fields["stock"].get()); threshold = int(fields["threshold"].get())
                if not name or price < 0 or stock < 0 or threshold < 0: raise ValueError()
            except ValueError:
                messagebox.showerror("Invalid product", "Enter a name, a non-negative price, and whole-number stock values.", parent=dialog); return
            vals = (name, price, stock, threshold, fields["description"].get().strip())
            if values:
                self.db.connection.execute("UPDATE products SET name=?,price=?,stock=?,low_stock_level=?,description=? WHERE id=?", vals + (int(values[0]),))
            else:
                if not self.confirm_action("Add product", "Add '{}' to the product catalog?".format(name),
                                           "Add product", parent=dialog):
                    return
                self.db.connection.execute("INSERT INTO products(name,price,stock,low_stock_level,description) VALUES(?,?,?,?,?)", vals)
            self.db.connection.commit(); dialog.destroy(); done()
        self.button(box, "Save product", save).pack(anchor="e", pady=(15, 0))

    def delete_product(self, view, refresh):
        selected = view.focus()
        if not selected: messagebox.showinfo("Select product", "Choose a product first."); return
        product_name = view.item(selected, "values")[1]
        if self.confirm_action("Delete product", "Delete '{}'? Past sale records will be retained.".format(product_name),
                               "Delete product", danger=True):
            self.db.connection.execute("DELETE FROM products WHERE id=?", (int(selected),)); self.db.connection.commit(); refresh()

    def page_services(self):
        self.heading("Services", "Maintain salon service descriptions and prices")
        panel = self.panel(); panel.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        toolbar = tk.Frame(panel, bg="white"); toolbar.pack(fill="x", pady=(0, 12))
        view = self.tree(panel, ("id", "name", "description", "price"), ("ID", "Service", "Description", "Price"), (60, 200, 420, 120))
        def refresh():
            view.delete(*view.get_children())
            for row in self.db.connection.execute("SELECT * FROM services ORDER BY name"):
                view.insert("", "end", iid=str(row["id"]), values=(row["id"], row["name"], row["description"], money(row["price"])))
        def edit():
            selected = view.focus(); values = view.item(selected, "values") if selected else None
            self.service_dialog(values, refresh)
        self.button(toolbar, "+ Add service", lambda: self.service_dialog(None, refresh)).pack(side="right", padx=3)
        self.button(toolbar, "Edit selected", edit, "secondary").pack(side="right", padx=3)
        self.button(toolbar, "Delete", lambda: self.delete_service(view, refresh), "danger").pack(side="right", padx=3)
        view.bind("<Double-1>", lambda event: edit())
        refresh()

    def service_dialog(self, values, done):
        dialog = tk.Toplevel(self); dialog.title("Service details"); dialog.transient(self); dialog.grab_set(); dialog.resizable(False, False)
        box = tk.Frame(dialog, bg="white", padx=24, pady=20); box.pack(fill="both", expand=True)
        entries = {}
        for key, label, default in [("name", "Service name", values[1] if values else ""),
                                    ("description", "Description", values[2] if values else ""),
                                    ("price", "Price (₱)", values[3].replace("₱", "").replace(",", "") if values else "")]:
            tk.Label(box, text=label, bg="white", font=("Arial", 9, "bold")).pack(anchor="w", pady=(8, 3))
            entry = tk.Entry(box, width=42); entry.pack(fill="x", ipady=5); entry.insert(0, default); entries[key] = entry
        def save():
            try:
                name = entries["name"].get().strip(); price = float(entries["price"].get())
                if not name or price < 0: raise ValueError()
            except ValueError:
                messagebox.showerror("Invalid service", "Enter a service name and non-negative price.", parent=dialog); return
            args = (name, entries["description"].get().strip(), price)
            if values: self.db.connection.execute("UPDATE services SET name=?,description=?,price=? WHERE id=?", args + (int(values[0]),))
            else:
                if not self.confirm_action("Add service", "Add '{}' to the services list?".format(name),
                                           "Add service", parent=dialog):
                    return
                self.db.connection.execute("INSERT INTO services(name,description,price) VALUES(?,?,?)", args)
            self.db.connection.commit(); dialog.destroy(); done()
        self.button(box, "Save service", save).pack(anchor="e", pady=(15, 0))

    def delete_service(self, view, refresh):
        selected = view.focus()
        if not selected: messagebox.showinfo("Select service", "Choose a service first."); return
        service_name = view.item(selected, "values")[1]
        if self.confirm_action("Delete service", "Remove '{}'? Past sale records will be retained.".format(service_name),
                               "Delete service", danger=True):
            self.db.connection.execute("DELETE FROM services WHERE id=?", (int(selected),)); self.db.connection.commit(); refresh()

    def page_users(self):
        self.heading("User management", "Create accounts and control administrator or cashier access")
        panel = self.panel(); panel.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        toolbar = tk.Frame(panel, bg="white"); toolbar.pack(fill="x", pady=(0, 12))
        view = self.tree(panel, ("id", "username", "email", "role", "status", "created"),
                         ("ID", "Username", "Email", "Role", "Status", "Created"), (55, 145, 190, 90, 85, 190))
        def refresh():
            view.delete(*view.get_children())
            for row in self.db.connection.execute("SELECT * FROM users ORDER BY username"):
                view.insert("", "end", iid=str(row["id"]), values=(row["id"], row["username"], row["email"], row["role"], "Active" if row["active"] else "Inactive", row["created_at"]))
        def selected_id():
            return int(view.focus()) if view.focus() else None
        self.button(toolbar, "+ Add user", lambda: self.user_dialog(None, refresh)).pack(side="right", padx=3)
        self.button(toolbar, "Edit / reset password", lambda: self.user_dialog(selected_id(), refresh), "secondary").pack(side="right", padx=3)
        self.button(toolbar, "Activate / deactivate", lambda: self.toggle_user(selected_id(), refresh), "secondary").pack(side="right", padx=3)
        self.button(toolbar, "Delete", lambda: self.delete_user(selected_id(), refresh), "danger").pack(side="right", padx=3)
        self.button(toolbar, "Generate recovery code", lambda: self.generate_recovery_code(selected_id()), "secondary").pack(side="right", padx=3)
        refresh()

    def user_dialog(self, user_id, done):
        row = self.db.connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
        dialog = tk.Toplevel(self); dialog.title("User account"); dialog.transient(self); dialog.grab_set(); dialog.resizable(False, False)
        box = tk.Frame(dialog, bg="white", padx=24, pady=20); box.pack(fill="both", expand=True)
        tk.Label(box, text="Username", bg="white", font=("Arial", 9, "bold")).pack(anchor="w", pady=(5, 3))
        username = tk.Entry(box, width=36); username.pack(fill="x", ipady=5)
        if row: username.insert(0, row["username"])
        tk.Label(box, text="Registered email (optional)", bg="white", font=("Arial", 9, "bold")).pack(anchor="w", pady=(10, 3))
        email = tk.Entry(box, width=36); email.pack(fill="x", ipady=5)
        if row: email.insert(0, row["email"])
        tk.Label(box, text="Password {}".format("(leave blank to keep current)" if row else ""), bg="white", font=("Arial", 9, "bold")).pack(anchor="w", pady=(10, 3))
        password = tk.Entry(box, width=36, show="•"); password.pack(fill="x", ipady=5)
        tk.Label(box, text="Role", bg="white", font=("Arial", 9, "bold")).pack(anchor="w", pady=(10, 3))
        role = ttk.Combobox(box, values=("Admin", "Cashier"), state="readonly"); role.pack(fill="x", ipady=3); role.set(row["role"] if row else "Cashier")
        active = tk.IntVar(value=row["active"] if row else 1)
        tk.Checkbutton(box, text="Account active", variable=active, bg="white").pack(anchor="w", pady=9)
        def save():
            name = username.get().strip(); secret = password.get(); registered_email = email.get().strip()
            if not name or (not row and len(secret) < 8):
                messagebox.showerror("Invalid account", "Enter a username and a password of at least 8 characters.", parent=dialog); return
            if registered_email and ("@" not in registered_email or "." not in registered_email.rsplit("@", 1)[-1]):
                messagebox.showerror("Invalid email", "Enter a valid email address or leave it blank.", parent=dialog); return
            try:
                if row:
                    if row["id"] == self.user["id"] and (role.get() != "Admin" or not active.get()):
                        messagebox.showerror("Account protection", "You cannot deactivate or demote your current admin account.", parent=dialog); return
                    if secret:
                        if len(secret) < 8: raise ValueError("Password must be at least 8 characters.")
                        self.db.connection.execute("UPDATE users SET username=?,email=?,password_hash=?,role=?,active=? WHERE id=?", (name, registered_email, make_password(secret), role.get(), active.get(), user_id))
                    else:
                        self.db.connection.execute("UPDATE users SET username=?,email=?,role=?,active=? WHERE id=?", (name, registered_email, role.get(), active.get(), user_id))
                else:
                    self.db.connection.execute("INSERT INTO users(username,email,password_hash,role,active,created_at) VALUES(?,?,?,?,?,?)", (name, registered_email, make_password(secret), role.get(), active.get(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                self.db.connection.commit()
            except sqlite3.IntegrityError:
                messagebox.showerror("Account details unavailable", "That username or email is already in use.", parent=dialog); return
            except ValueError as error:
                messagebox.showerror("Invalid password", str(error), parent=dialog); return
            dialog.destroy(); done()
        self.button(box, "Save user", save).pack(anchor="e", pady=(8, 0))

    def generate_recovery_code(self, user_id):
        if not user_id:
            messagebox.showinfo("Select user", "Choose an account first.")
            return
        row = self.db.connection.execute(
            "SELECT username, active FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            messagebox.showerror("Account unavailable", "That account no longer exists.")
            return
        if not row["active"]:
            messagebox.showerror("Account inactive", "Activate this account before generating a recovery code.")
            return
        code = secrets.token_urlsafe(12)
        self.db.connection.execute(
            "UPDATE users SET recovery_code_hash=? WHERE id=?",
            (make_password(code), user_id))
        self.db.connection.commit()
        messagebox.showinfo(
            "Recovery code generated",
            "Give this one-time code to {} through a trusted, separate channel. "
            "It will be shown only once and is invalidated after a password reset.\n\n{}".format(
                row["username"], code), parent=self)

    def toggle_user(self, user_id, done):
        if not user_id: messagebox.showinfo("Select user", "Choose a user first."); return
        if user_id == self.user["id"]: messagebox.showerror("Account protection", "You cannot deactivate your current account."); return
        row = self.db.connection.execute("SELECT active FROM users WHERE id=?", (user_id,)).fetchone()
        self.db.connection.execute("UPDATE users SET active=? WHERE id=?", (0 if row["active"] else 1, user_id)); self.db.connection.commit(); done()

    def delete_user(self, user_id, done):
        if not user_id: messagebox.showinfo("Select user", "Choose a user first."); return
        if user_id == self.user["id"]: messagebox.showerror("Account protection", "You cannot delete your current account."); return
        row = self.db.connection.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if row["role"] == "Admin" and self.db.connection.execute("SELECT COUNT(*) FROM users WHERE role='Admin' AND active=1").fetchone()[0] <= 1:
            messagebox.showerror("Account protection", "At least one active administrator must remain."); return
        if messagebox.askyesno("Delete account", "Delete this user account? Historical sales remain saved."):
            self.db.connection.execute("DELETE FROM users WHERE id=?", (user_id,)); self.db.connection.commit(); done()

    def page_sales(self):
        self.heading("Transactions", "Search sales history and inspect transaction line items")
        panel = self.panel(); panel.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        bar = tk.Frame(panel, bg="white"); bar.pack(fill="x", pady=(0, 12))
        search = tk.Entry(bar, width=24, relief="solid", bd=1); search.pack(side="left", ipady=6); search.insert(0, "Sale ID or cashier")
        period = ttk.Combobox(bar, values=("All dates", "Today", "This week", "This month"), state="readonly", width=14); period.set("All dates"); period.pack(side="left", padx=8)
        view = self.tree(panel, ("id", "date", "user", "items", "total"), ("Sale #", "Date", "Cashier", "Items", "Total"), (70, 210, 150, 300, 110))
        def refresh():
            view.delete(*view.get_children())
            term = search.get().strip(); term = "" if term == "Sale ID or cashier" else term
            clause = ""
            if period.get() == "Today": clause = " AND date(s.sold_at)=date('now','localtime')"
            elif period.get() == "This week": clause = " AND date(s.sold_at)>=date('now','-6 days','localtime')"
            elif period.get() == "This month": clause = " AND strftime('%Y-%m',s.sold_at)=strftime('%Y-%m','now','localtime')"
            rows = self.db.connection.execute("""SELECT s.id,s.sold_at,s.total,COALESCE(u.username,'Deleted user') username,
                (SELECT group_concat(item_name || ' × ' || quantity, ', ') FROM sale_items WHERE sale_id=s.id) items
                FROM sales s LEFT JOIN users u ON u.id=s.user_id WHERE (CAST(s.id AS TEXT) LIKE ? OR u.username LIKE ?)""" + clause + " ORDER BY s.id DESC",
                ("%{}%".format(term), "%{}%".format(term))).fetchall()
            for row in rows: view.insert("", "end", iid=str(row["id"]), values=(row["id"], row["sold_at"], row["username"], row["items"] or "—", money(row["total"])))
        self.button(bar, "Apply filters", refresh, "secondary").pack(side="left")
        self.button(bar, "View details", lambda: self.sale_details(view.focus()), "primary").pack(side="right")
        view.bind("<Double-1>", lambda event: self.sale_details(view.focus()))
        search.bind("<Return>", lambda event: refresh()); period.bind("<<ComboboxSelected>>", lambda event: refresh())
        refresh()

    def sale_details(self, sale_id):
        if not sale_id: messagebox.showinfo("Select sale", "Choose a transaction first."); return
        row = self.db.connection.execute("SELECT * FROM sales WHERE id=?", (int(sale_id),)).fetchone()
        lines = self.db.connection.execute("SELECT * FROM sale_items WHERE sale_id=?", (int(sale_id),)).fetchall()
        details = "Sale #{}\nDate: {}\n\n".format(row["id"], row["sold_at"])
        details += "\n".join("{}  |  {} × {}  |  {}".format(line["item_type"].title(), line["item_name"], line["quantity"], money(line["unit_price"] * line["quantity"])) for line in lines)
        details += "\n\nTotal: {}".format(money(row["total"]))
        messagebox.showinfo("Transaction details", details)

    def page_pos(self):
        self.heading("New sale", "Add products and services, then complete checkout")
        body = tk.Frame(self.content, bg=self.colors["bg"]); body.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        left = self.panel(body); left.pack(side="left", fill="both", expand=True, padx=(0, 10))
        right = self.panel(body); right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        tk.Label(left, text="Add an item", bg="white", fg=self.colors["ink"], font=("Arial", 13, "bold")).pack(anchor="w", pady=(0, 14))
        kind = ttk.Combobox(left, values=("Product", "Service"), state="readonly"); kind.set("Product"); kind.pack(fill="x", pady=5)
        item = ttk.Combobox(left, state="readonly"); item.pack(fill="x", pady=5)
        quantity = tk.Spinbox(left, from_=1, to=9999, width=8); quantity.pack(anchor="w", pady=8)
        view = ttk.Treeview(right, columns=("name", "qty", "price"), show="headings", height=13)
        for col, title, width in [("name", "Item", 175), ("qty", "Qty", 55), ("price", "Line total", 110)]: view.heading(col, text=title); view.column(col, width=width)
        view.pack(fill="both", expand=True)
        total_label = tk.Label(right, text="Total: ₱0.00", bg="white", fg=self.colors["ink"], font=("Arial", 14, "bold")); total_label.pack(anchor="e", pady=10)
        choices = []
        def fill_items(event=None):
            table = "products" if kind.get() == "Product" else "services"
            choices[:] = self.db.connection.execute("SELECT * FROM " + table + (" WHERE stock>0" if table == "products" else "") + " ORDER BY name").fetchall()
            item["values"] = ["{} — {}".format(row["name"], money(row["price"])) for row in choices]
            item.set(choices and item["values"][0] or "No items available")
        def refresh_cart():
            view.delete(*view.get_children())
            total = sum(line["qty"] * line["price"] for line in self.cart)
            for i, line in enumerate(self.cart): view.insert("", "end", iid=str(i), values=(line["name"], line["qty"], money(line["qty"] * line["price"])))
            total_label.config(text="Total: {}".format(money(total)))
        def add():
            if not choices: messagebox.showerror("No items", "Add a product or service before checkout."); return
            index = item.current()
            if index < 0 or index >= len(choices): return
            selected = choices[index]
            try: qty = int(quantity.get())
            except ValueError: messagebox.showerror("Invalid quantity", "Enter a whole-number quantity."); return
            if qty < 1: messagebox.showerror("Invalid quantity", "Quantity must be at least 1."); return
            product = kind.get() == "Product"
            existing = sum(line["qty"] for line in self.cart if line["type"] == ("product" if product else "service") and line["id"] == selected["id"])
            if product and existing + qty > selected["stock"]: messagebox.showerror("Insufficient stock", "Available stock: {}".format(max(0, selected["stock"] - existing))); return
            self.cart.append({"type": "product" if product else "service", "id": selected["id"], "name": selected["name"], "qty": qty if product else 1, "price": selected["price"]})
            refresh_cart()
        def remove():
            chosen = view.focus()
            if chosen: self.cart.pop(int(chosen)); refresh_cart()
        def complete():
            if not self.cart: messagebox.showinfo("Empty sale", "Add items before completing the sale."); return
            if not messagebox.askyesno("Complete sale", "Complete this sale for {}?".format(total_label.cget("text").replace("Total: ", ""))): return
            conn = self.db.connection
            try:
                conn.execute("BEGIN")
                total = sum(line["qty"] * line["price"] for line in self.cart)
                cursor = conn.execute("INSERT INTO sales(sold_at,total,user_id) VALUES(?,?,?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), round(total, 2), self.user["id"]))
                sale_id = cursor.lastrowid
                for line in self.cart:
                    if line["type"] == "product":
                        updated = conn.execute("UPDATE products SET stock=stock-? WHERE id=? AND stock>=?", (line["qty"], line["id"], line["qty"])).rowcount
                        if not updated: raise ValueError("Stock changed or is no longer sufficient for {}.".format(line["name"]))
                    conn.execute("INSERT INTO sale_items(sale_id,item_type,item_id,item_name,quantity,unit_price) VALUES(?,?,?,?,?,?)", (sale_id, line["type"], line["id"], line["name"], line["qty"], line["price"]))
                conn.commit()
            except (sqlite3.Error, ValueError) as error:
                conn.rollback(); messagebox.showerror("Sale not completed", str(error)); return
            self.cart = []; refresh_cart(); fill_items()
            messagebox.showinfo("Sale completed", "Sale #{} saved.".format(sale_id))
        kind.bind("<<ComboboxSelected>>", fill_items)
        self.button(left, "Add to sale", add).pack(anchor="w", pady=8)
        self.button(right, "Remove selected", remove, "danger").pack(side="left")
        self.button(right, "Complete sale", complete).pack(side="right")
        fill_items()

    def page_reports(self):
        self.heading("Reports", "Daily, weekly, monthly sales and inventory performance")
        body = tk.Frame(self.content, bg=self.colors["bg"]); body.pack(fill="both", expand=True, padx=28, pady=(0, 24))
        today = date.today()
        start_week = today - timedelta(days=today.weekday())
        start_month = today.replace(day=1)
        spans = [("Today", today.isoformat()), ("This week", start_week.isoformat()), ("This month", start_month.isoformat())]
        grid = tk.Frame(body, bg=self.colors["bg"]); grid.pack(fill="x")
        for index, (label, start) in enumerate(spans):
            amount, count = self.db.connection.execute("SELECT COALESCE(SUM(total),0),COUNT(*) FROM sales WHERE date(sold_at)>=?", (start,)).fetchone()
            card = self.panel(grid); card.grid(row=0, column=index, sticky="nsew", padx=6)
            tk.Label(card, text=label + " sales", bg="white", fg=self.colors["muted"]).pack(anchor="w")
            tk.Label(card, text=money(amount), bg="white", fg=self.colors["ink"], font=("Arial", 20, "bold")).pack(anchor="w", pady=8)
            tk.Label(card, text="{} transactions".format(count), bg="white", fg=self.colors["muted"]).pack(anchor="w")
            grid.grid_columnconfigure(index, weight=1)
        lower = tk.Frame(body, bg=self.colors["bg"]); lower.pack(fill="both", expand=True, pady=12)
        for title, query, columns, labels in [
            ("Best-selling products", "SELECT item_name,SUM(quantity) qty,SUM(quantity*unit_price) revenue FROM sale_items WHERE item_type='product' GROUP BY item_name ORDER BY qty DESC LIMIT 8", ("name", "qty", "revenue"), ("Product", "Sold", "Revenue")),
            ("Most popular services", "SELECT item_name,SUM(quantity) qty,SUM(quantity*unit_price) revenue FROM sale_items WHERE item_type='service' GROUP BY item_name ORDER BY qty DESC LIMIT 8", ("name", "qty", "revenue"), ("Service", "Booked", "Revenue"))]:
            panel = self.panel(lower); panel.pack(side="left", fill="both", expand=True, padx=6)
            tk.Label(panel, text=title, bg="white", fg=self.colors["ink"], font=("Arial", 12, "bold")).pack(anchor="w", pady=(0, 10))
            view = self.tree(panel, columns, labels, (190, 65, 110))
            for row in self.db.connection.execute(query): view.insert("", "end", values=(row["item_name"], row["qty"], money(row["revenue"])))
        inventory = self.panel(body); inventory.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        tk.Label(inventory, text="Inventory report · low stock is highlighted", bg="white", fg=self.colors["ink"], font=("Arial", 12, "bold")).pack(anchor="w", pady=(0, 8))
        view = self.tree(inventory, ("name", "stock", "threshold", "price"), ("Product", "In stock", "Low-stock at", "Unit price"), (240, 100, 120, 120))
        for row in self.db.connection.execute("SELECT * FROM products ORDER BY stock ASC"):
            view.insert("", "end", values=(row["name"], row["stock"], row["low_stock_level"], money(row["price"])), tags=("low",) if row["stock"] <= row["low_stock_level"] else ())
        view.tag_configure("low", foreground="#b74450")

    def page_settings(self):
        self.heading("System settings", "Salon profile, data backup, and administrator security")
        panel = self.panel(); panel.pack(fill="x", padx=28, pady=(0, 14))
        tk.Label(panel, text="Salon information", bg="white", fg=self.colors["ink"], font=("Arial", 13, "bold")).pack(anchor="w", pady=(0, 10))
        name = tk.Entry(panel, width=50); name.pack(anchor="w", ipady=5); name.insert(0, self.db.get_setting("salon_name", "CB SPOSIS"))
        self.button(panel, "Save salon name", lambda: self.db.set_setting("salon_name", name.get().strip()) if name.get().strip() else messagebox.showerror("Invalid name", "Salon name cannot be empty.")).pack(anchor="w", pady=(8, 0))
        security = self.panel(); security.pack(fill="x", padx=28, pady=8)
        tk.Label(security, text="Administrator password", bg="white", fg=self.colors["ink"], font=("Arial", 13, "bold")).pack(anchor="w", pady=(0, 8))
        self.button(security, "Change my password", self.change_password, "secondary").pack(anchor="w")
        backup = self.panel(); backup.pack(fill="x", padx=28, pady=8)
        tk.Label(backup, text="Database backup and restore", bg="white", fg=self.colors["ink"], font=("Arial", 13, "bold")).pack(anchor="w", pady=(0, 8))
        actions = tk.Frame(backup, bg="white"); actions.pack(anchor="w")
        self.button(actions, "Back up database", self.backup_database).pack(side="left", padx=(0, 8))
        self.button(actions, "Restore backup", self.restore_database, "secondary").pack(side="left")
        tk.Label(backup, text="Database file: {}".format(os.path.abspath(DB_FILE)), bg="white", fg=self.colors["muted"]).pack(anchor="w", pady=(8, 0))

    def change_password(self):
        current = simpledialog.askstring("Change password", "Current password:", show="•", parent=self)
        if current is None: return
        row = self.db.connection.execute("SELECT password_hash FROM users WHERE id=?", (self.user["id"],)).fetchone()
        if not check_password(current, row["password_hash"]): messagebox.showerror("Incorrect password", "Current password does not match."); return
        new = simpledialog.askstring("Change password", "New password (at least 8 characters):", show="•", parent=self)
        if new is None: return
        if len(new) < 8: messagebox.showerror("Invalid password", "Use at least 8 characters."); return
        confirm = simpledialog.askstring("Confirm password", "Enter the new password again:", show="•", parent=self)
        if new != confirm: messagebox.showerror("Password mismatch", "The passwords do not match."); return
        self.db.connection.execute("UPDATE users SET password_hash=? WHERE id=?", (make_password(new), self.user["id"])); self.db.connection.commit()
        messagebox.showinfo("Password updated", "Your password has been changed.")

    def backup_database(self):
        path = filedialog.asksaveasfilename(title="Save database backup", defaultextension=".db", filetypes=[("SQLite database", "*.db")], initialfile="salon_pos_backup.db")
        if not path: return
        self.db.connection.commit()
        target = sqlite3.connect(path)
        try: self.db.connection.backup(target)
        finally: target.close()
        messagebox.showinfo("Backup complete", "Database backup saved.")

    def restore_database(self):
        path = filedialog.askopenfilename(title="Choose database backup", filetypes=[("SQLite database", "*.db"), ("All files", "*.*")])
        if not path: return
        if os.path.abspath(path) == os.path.abspath(DB_FILE): messagebox.showerror("Invalid backup", "Choose a backup file other than the active database."); return
        if not messagebox.askyesno("Restore database", "Replace current salon data with this backup? This cannot be undone."): return
        try:
            check = sqlite3.connect(path)
            required = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            check.close()
            if not {"users", "products", "services", "sales", "sale_items", "settings"}.issubset(required): raise ValueError("This file is not a compatible Salon POS backup.")
            self.db.connection.close()
            shutil.copy2(path, DB_FILE)
            self.db = SalonDatabase()
            messagebox.showinfo("Restore complete", "Database restored. Please sign in again.")
            self.show_login()
        except (sqlite3.Error, IOError, ValueError) as error:
            messagebox.showerror("Restore failed", str(error))
            self.db = SalonDatabase()

    def on_close(self):
        try: self.db.connection.close()
        except sqlite3.Error: pass
        self.destroy()


def main():
    app = SalonApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
