import os

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

# Enable plotting by default so the WebView has something to show
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


# --- Custom Logging Handler ---
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


class TemoaGUI(toga.App):
    def startup(self):
        # --- State ---
        self.source_path: Path | None = None
        self.toml_doc: tomlkit.TOMLDocument | None = None

        # --- UI Layout ---
        # Main padding for the whole input area
        input_box = toga.Box(style=Pack(direction=COLUMN, margin=15))

        # 1. File Selection
        file_row = toga.Box(style=Pack(direction=ROW, margin_bottom=15))
        self.path_input = toga.TextInput(
            readonly=True,
            placeholder="Select .toml config or .sqlite database...",
            style=Pack(flex=1, margin_right=10),
        )
        btn_browse = toga.Button(
            "Browse", on_press=self.select_file, style=Pack(width=90)
        )

        # Label for file input
        input_box.add(
            toga.Label("Input Source", style=Pack(margin_bottom=5, font_weight="bold"))
        )
        file_row.add(self.path_input)
        file_row.add(btn_browse)
        input_box.add(file_row)

        input_box.add(toga.Divider(style=Pack(margin_bottom=15)))
        input_box.add(
            toga.Label(
                "Settings Override", style=Pack(margin_bottom=10, font_weight="bold")
            )
        )

        # 2. Configuration Grid
        # We use a helper function to create consistent "Label Above Field" columns
        def build_field(label_text, widget, right_margin=0):
            # Container for the field
            box = toga.Box(
                style=Pack(direction=COLUMN, flex=1, margin_right=right_margin)
            )
            # Label styled to be small and above
            label = toga.Label(
                label_text, style=Pack(margin_bottom=3, font_size=8, color="#666666")
            )
            # Ensure widget stretches
            widget.style.flex = 1
            box.add(label)
            box.add(widget)
            return box

        # Grid Row 1
        row_a = toga.Box(style=Pack(direction=ROW, margin_bottom=15))

        # Mode Selection
        self.mode_select = toga.Selection(items=MODES)
        # Solver Selection
        self.solver_select = toga.Selection(items=SOLVERS)

        # Add to Row A with spacing
        row_a.add(build_field("SCENARIO MODE", self.mode_select, right_margin=20))
        row_a.add(build_field("SOLVER", self.solver_select))

        # Grid Row 2
        row_b = toga.Box(style=Pack(direction=ROW, margin_bottom=20))
        # Time Sequencing
        self.time_select = toga.Selection(items=TIME_SEQUENCES)
        row_b.add(build_field("TIME SEQUENCING", self.time_select))

        input_box.add(row_a)
        input_box.add(row_b)

        # Run Button Area
        run_box = toga.Box(style=Pack(direction=ROW, alignment="center"))
        self.btn_run = toga.Button(
            "RUN MODEL",
            on_press=self.run_model,
            enabled=False,
            style=Pack(flex=1, height=45),
        )
        self.spinner = toga.ActivityIndicator(style=Pack(margin_left=15))

        run_box.add(self.btn_run)
        run_box.add(self.spinner)
        input_box.add(run_box)

        # 3. Output Tabs
        self.content_container = toga.OptionContainer(style=Pack(flex=1))
        self.log_view = toga.MultilineTextInput(
            readonly=True, style=Pack(flex=1, font_family="monospace")
        )

        self.webview = toga.WebView(url="https://temoaproject.org", style=Pack(flex=1))

        self.content_container.content.append(toga.OptionItem("Logs", self.log_view))
        self.content_container.content.append(toga.OptionItem("Report", self.webview))

        # Main Window
        main_box = toga.Box(style=Pack(direction=COLUMN))
        main_box.add(input_box)
        main_box.add(self.content_container)

        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = main_box
        self.main_window.show()

    # --- Actions ---

    async def select_file(self, widget):
        try:
            fname = await self.main_window.dialog(
                toga.OpenFileDialog(title="Select Input", file_types=["toml", "sqlite"])
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
            if path.suffix in [".sqlite"]:
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

            # Update UI
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

            html_files = list(output_path.glob("*.html"))
            svg_files = list(output_path.glob("*.svg"))

            if html_files:
                try:
                    content = html_files[0].read_text(encoding="utf-8")
                    self.webview.set_content(
                        f"file://{html_files[0].absolute()}", content
                    )
                    self.content_container.current_tab = 1
                except Exception as e:
                    self.log_view.value += f"\nError loading HTML content: {e}"
            elif svg_files:
                try:
                    svg_content = svg_files[0].read_text(encoding="utf-8")
                    html_wrapper = f"<html><body>{svg_content}</body></html>"
                    self.webview.set_content(
                        f"file://{svg_files[0].absolute()}", html_wrapper
                    )
                    self.content_container.current_tab = 1
                except Exception as e:
                    self.log_view.value += f"\nError loading SVG content: {e}"
            else:
                self.log_view.value += "No visual report found in output directory."
        else:
            self.log_view.value += "\n❌ Optimization Failed. See logs above."


def main():
    return TemoaGUI()
