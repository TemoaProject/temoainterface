import os
import sys
import socket
import contextlib
import http.server
import socketserver
import threading

# --- CRITICAL LINUX FIX ---
if os.name == "posix":
    os.environ["WEBKIT_DISABLE_COMPOSITING_MODE"] = "1"

import asyncio
import logging
import tomlkit
from datetime import datetime
from pathlib import Path

import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW

# --- Temoa Imports ---
from temoa._internal.temoa_sequencer import TemoaSequencer
from temoa.core.config import TemoaConfig

# --- Datasette Imports ---
from datasette.app import Datasette

# --- Constants ---
MODES = [
    "perfect_foresight",
    "MGA",
    "myopic",
    "method_of_morris",
    "build_only",
    "check",
    "monte_carlo",
]
SOLVERS = ["appsi_highs", "cbc", "gurobi", "cplex"]
TIME_SEQUENCES = [
    "seasonal_timeslices",
    "consecutive_days",
    "representative_periods",
    "manual",
]

DEFAULT_TOML_TEMPLATE = """
scenario = "gui_run"
scenario_mode = "perfect_foresight"
solver_name = "appsi_highs"
time_sequencing = "seasonal_timeslices"
save_excel = true
price_check = true
source_trace = true
plot_commodity_network = true
"""


# --- Helper: Find Free Port ---
def find_free_port():
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


# --- Servers ---

class DatasetteServer:
    """Manages a background Datasette instance."""
    def __init__(self):
        self.server_task = None
        self.port = None
        self.running = False

    async def start(self, db_path: Path):
        if self.running:
            return f"http://127.0.0.1:{self.port}"

        self.port = find_free_port()
        self.running = True

        ds = Datasette(files=[str(db_path)], immutables=[], settings={"base_url": "/"})
        import uvicorn
        config = uvicorn.Config(
            ds.app(), host="127.0.0.1", port=self.port, log_level="error"
        )
        server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.5)
        return f"http://127.0.0.1:{self.port}"

class StaticFileServer:
    """Serves the output directory over HTTP to avoid file:// protocol issues."""
    def __init__(self):
        self.server = None
        self.thread = None
        self.port = None
        self.root_dir = None

    def start(self, root_dir: Path):
        # If we are already serving this exact dir, return existing URL
        if self.server and self.root_dir == root_dir:
             return f"http://127.0.0.1:{self.port}"

        # Stop previous server if it exists
        if self.server:
            self.stop()

        self.root_dir = root_dir
        self.port = find_free_port()

        # Define a handler that serves the specific directory silently
        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(root_dir), **kwargs)
            def log_message(self, format, *args):
                pass # Silence console logs

        self.server = socketserver.TCPServer(("127.0.0.1", self.port), Handler)

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


# --- Logging ---
class TogaLogHandler(logging.Handler):
    def __init__(self, app, log_widget):
        super().__init__()
        self.app = app
        self.log_widget = log_widget
        self.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
        )

    def emit(self, record):
        msg = self.format(record)
        self.app.loop.call_soon_threadsafe(self._append_text, msg + "\n")

    def _append_text(self, text):
        self.log_widget.value += text
        self.log_widget.scroll_to_bottom()


class StreamRedirector:
    def __init__(self, app, log_widget, prefix=""):
        self.app = app
        self.log_widget = log_widget
        self.prefix = prefix

    def write(self, text):
        if text.strip():
            self.app.loop.call_soon_threadsafe(self._append, f"{self.prefix}{text}")

    def flush(self):
        pass

    def _append(self, text):
        self.log_widget.value += text
        self.log_widget.scroll_to_bottom()


class TemoaGUI(toga.App):
    def startup(self):
        # --- State ---
        self.source_path: Path | None = None
        self.toml_doc: tomlkit.TOMLDocument | None = None
        self.ds_server = DatasetteServer()
        self.static_server = StaticFileServer()

        # ==========================
        # LEFT SIDEBAR: Configuration
        # ==========================
        sidebar_box = toga.Box(style=Pack(direction=COLUMN, padding=10))

        # 1. Header
        sidebar_box.add(toga.Label("CONFIGURATION", style=Pack(font_weight="bold", margin_bottom=10)))

        # 2. Input File
        sidebar_box.add(toga.Label("Input Source:", style=Pack(margin_bottom=2, font_size=9)))
        self.path_input = toga.TextInput(
            readonly=True,
            placeholder="Select file...",
            style=Pack(margin_bottom=5)
        )
        btn_browse = toga.Button("Browse...", on_press=self.select_file)

        sidebar_box.add(self.path_input)
        sidebar_box.add(btn_browse)
        sidebar_box.add(toga.Divider(style=Pack(margin_top=15, margin_bottom=15)))

        # 3. Settings (Vertical Stack)
        sidebar_box.add(toga.Label("Run Settings:", style=Pack(margin_bottom=10, font_weight="bold", font_size=10)))

        def build_sidebar_field(label_text, widget):
            box = toga.Box(style=Pack(direction=COLUMN, margin_bottom=12))
            label = toga.Label(label_text, style=Pack(margin_bottom=2, font_size=9, color="#666666"))
            box.add(label)
            box.add(widget)
            return box

        self.mode_select = toga.Selection(items=MODES)
        self.solver_select = toga.Selection(items=SOLVERS)
        self.time_select = toga.Selection(items=TIME_SEQUENCES)

        sidebar_box.add(build_sidebar_field("SCENARIO MODE", self.mode_select))
        sidebar_box.add(build_sidebar_field("SOLVER", self.solver_select))
        sidebar_box.add(build_sidebar_field("TIME SEQUENCING", self.time_select))

        # Spacer to push Run button to bottom
        sidebar_box.add(toga.Box(style=Pack(flex=1)))

        # 4. Run Controls
        self.spinner = toga.ActivityIndicator(style=Pack(margin_bottom=5))
        self.btn_run = toga.Button(
            "RUN MODEL",
            on_press=self.run_model,
            enabled=False,
            style=Pack(height=40, font_weight="bold")
        )

        sidebar_box.add(self.spinner)
        sidebar_box.add(self.btn_run)

        # ==========================
        # RIGHT CONTENT: Output Tabs
        # ==========================
        self.content_container = toga.OptionContainer(style=Pack(flex=1))

        # Tab 1: Logs
        self.log_view = toga.MultilineTextInput(
            readonly=True, style=Pack(flex=1, font_family="monospace")
        )

        # Tab 2: Visual Report
        self.report_webview = toga.WebView(
            url="https://temoaproject.org", style=Pack(flex=1)
        )

        # Tab 3: Database Inspector
        self.db_webview = toga.WebView(
            url="https://temoaproject.org", style=Pack(flex=1)
        )

        # DB Nav Bar
        nav_box = toga.Box(
            style=Pack(direction=ROW, margin_bottom=5, align_items="center")
        )
        self.btn_back = toga.Button("Back", on_press=lambda w: self.db_webview.evaluate_javascript("history.back()"), style=Pack(width=60, margin_right=5))
        self.btn_fwd = toga.Button("Fwd", on_press=lambda w: self.db_webview.evaluate_javascript("history.forward()"), style=Pack(width=60, margin_right=5))
        self.btn_reload = toga.Button("Reload", on_press=lambda w: self.db_webview.evaluate_javascript("location.reload()"), style=Pack(width=70))

        nav_box.add(self.btn_back)
        nav_box.add(self.btn_fwd)
        nav_box.add(self.btn_reload)

        db_container = toga.Box(style=Pack(direction=COLUMN, margin=10))
        db_container.add(nav_box)
        db_container.add(self.db_webview)

        self.content_container.content.append(toga.OptionItem("Logs", self.log_view))
        self.content_container.content.append(toga.OptionItem("Visual Report", self.report_webview))
        self.content_container.content.append(toga.OptionItem("Database Inspector", db_container))

        # ==========================
        # ROOT: Split Container
        # ==========================
        # SplitContainer holds [Left, Right].
        # We give the sidebar a fixed initial weight to control width.
        split = toga.SplitContainer(content=[sidebar_box, self.content_container])

        # Initial sizing is tricky in Toga cross-platform, but setting direction helps.
        # Note: Toga SplitContainer defaults to 50/50 split usually.
        # User can resize or collapse it manually.

        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = split
        self.main_window.show()

    # --- Actions ---
    async def select_file(self, widget):
        try:
            fname = await self.main_window.dialog(
                toga.OpenFileDialog(
                    title="Select Input", file_types=["toml", "sqlite", "db", "dat"]
                )
            )
            if fname:
                self.source_path = fname
                self.path_input.value = fname.name
                self._load_config_logic(fname)
                self.btn_run.enabled = True
        except ValueError:
            pass

    def _load_config_logic(self, path: Path):
        try:
            if path.suffix in [".sqlite", ".db"]:
                self.toml_doc = tomlkit.parse(DEFAULT_TOML_TEMPLATE)
                self.toml_doc["input_database"] = str(path.absolute())
                self.toml_doc["output_database"] = str(path.absolute())
            else:
                content = path.read_text(encoding="utf-8")
                self.toml_doc = tomlkit.parse(content)

                for db_key in ["input_database", "output_database"]:
                    if db_key in self.toml_doc:
                        p = Path(self.toml_doc[db_key])
                        if not p.is_absolute():
                            abs_p = (path.parent / p).resolve()
                            self.toml_doc[db_key] = str(abs_p)

            if "scenario_mode" in self.toml_doc:
                val = self.toml_doc["scenario_mode"]
                if val in MODES:
                    self.mode_select.value = val

            if "solver_name" in self.toml_doc:
                val = self.toml_doc["solver_name"]
                if val in SOLVERS:
                    self.solver_select.value = val

            if "time_sequencing" in self.toml_doc:
                val = self.toml_doc["time_sequencing"]
                if val in TIME_SEQUENCES:
                    self.time_select.value = val

        except Exception as e:
            self.main_window.error_dialog("Error loading file", str(e))

    async def run_model(self, widget):
        if not self.toml_doc:
            return

        self.btn_run.enabled = False
        self.spinner.start()
        self.log_view.value = (
            f"--- Starting Run: {datetime.now().strftime('%H:%M:%S')} ---\n"
        )
        self.content_container.current_tab = 0

        self.toml_doc["scenario_mode"] = self.mode_select.value
        self.toml_doc["solver_name"] = self.solver_select.value
        self.toml_doc["time_sequencing"] = self.time_select.value

        await asyncio.to_thread(self._execute_temoa_logic)

    def _execute_temoa_logic(self):
        original_stdout = sys.stdout
        original_stderr = sys.stderr

        sys.stdout = StreamRedirector(self.app, self.log_view, prefix="")
        sys.stderr = StreamRedirector(self.app, self.log_view, prefix="[STDERR] ")

        root_logger = logging.getLogger()
        gui_handler = TogaLogHandler(self.app, self.log_view)
        root_logger.addHandler(gui_handler)

        logging.getLogger("temoa").setLevel(logging.INFO)
        logging.getLogger("pyomo").setLevel(logging.WARNING)
        logging.getLogger("matplotlib").setLevel(logging.WARNING)

        final_output_path = None
        success = False

        try:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

            if self.source_path:
                base_parent = self.source_path.parent
            else:
                base_parent = Path.cwd()

            output_dir = base_parent / "output_files" / timestamp
            output_dir.mkdir(parents=True, exist_ok=True)

            self.app.loop.call_soon_threadsafe(
                self._append_log, f"Output directory: {output_dir}\n"
            )

            run_config_path = output_dir / "run_config.toml"
            with open(run_config_path, "w", encoding="utf-8") as f:
                f.write(tomlkit.dumps(self.toml_doc))

            self.app.loop.call_soon_threadsafe(
                self._append_log, f"Configuration saved to: {run_config_path.name}\n"
            )

            config = TemoaConfig.build_config(
                config_file=run_config_path, output_path=output_dir, silent=False
            )

            sequencer = TemoaSequencer(config=config)
            sequencer.start()

            final_output_path = output_dir
            success = True

        except Exception:
            import traceback
            tb = traceback.format_exc()
            self.app.loop.call_soon_threadsafe(self._append_log, f"\nERROR:\n{tb}\n")

        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            root_logger.removeHandler(gui_handler)

            self.app.loop.call_soon_threadsafe(
                self._on_run_complete, success, final_output_path
            )

    def _append_log(self, text):
        self.log_view.value += text
        self.log_view.scroll_to_bottom()

    def _on_run_complete(self, success, output_path: Path | None):
        self.spinner.stop()
        self.btn_run.enabled = True

        if success and output_path:
            self.log_view.value += "\n✅ Optimization Complete.\n"

            # 1. Handle Visual Report via Static Server
            html_files = list(output_path.glob("*.html"))
            svg_files = list(output_path.glob("*.svg"))

            try:
                server_url = self.static_server.start(output_path)

                target_file = None
                if html_files:
                    target_file = html_files[0].name
                elif svg_files:
                    target_file = svg_files[0].name

                if target_file:
                    full_url = f"{server_url}/{target_file}"
                    self.log_view.value += f"Loading report at: {full_url}\n"
                    self.report_webview.url = full_url
                    self.content_container.current_tab = 1
                else:
                    self.log_view.value += "No visual report file found in output."

            except Exception as e:
                self.log_view.value += f"\nError starting report server: {e}"

            # 2. Handle Database Inspector
            sqlite_files = list(output_path.glob("*.sqlite"))
            if sqlite_files:
                db_path = sqlite_files[0]
                self.log_view.value += (
                    f"\nLaunching Database Inspector for: {db_path.name}...\n"
                )
                asyncio.create_task(self._launch_datasette(db_path))
            else:
                db_path_str = self.toml_doc.get("output_database")
                if db_path_str:
                    db_path = Path(db_path_str)
                    if db_path.exists():
                        asyncio.create_task(self._launch_datasette(db_path))

        else:
            self.log_view.value += "\n❌ Optimization Failed. See logs above."

    async def _launch_datasette(self, db_path):
        try:
            url = await self.ds_server.start(db_path)
            self.db_webview.url = url
            self.log_view.value += f"Database Inspector ready at {url}\n"
        except Exception as e:
            self.log_view.value += f"\nFailed to start Datasette: {e}\n"


def main():
    return TemoaGUI()