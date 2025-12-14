import os

# --- CRITICAL FIX FOR LINUX WEBVIEW ---
# Disables hardware acceleration for WebKitGTK to prevent "GBM buffer" crashes.
# This must be done before importing toga.
if os.name == "posix":
    os.environ["WEBKIT_DISABLE_COMPOSITING_MODE"] = "1"

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW

# --- Temoa Imports ---
from temoa._internal.temoa_sequencer import TemoaSequencer
from temoa.core.config import TemoaConfig


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
        """Construct and show the Toga application."""

        # --- State ---
        self.config_path: Path | None = None

        # --- UI Layout ---

        # 1. Input Section
        input_box = toga.Box(style=Pack(direction=COLUMN, margin=10))

        # Config Selection
        file_row = toga.Box(style=Pack(direction=ROW, margin_bottom=5))
        self.path_input = toga.TextInput(
            readonly=True,
            placeholder="Select configuration file...",
            style=Pack(flex=1, margin_right=5),
        )
        btn_browse = toga.Button(
            "Browse", on_press=self.select_file, style=Pack(width=100)
        )
        file_row.add(self.path_input)
        file_row.add(btn_browse)

        # Controls
        control_row = toga.Box(style=Pack(direction=ROW, margin_top=5))
        self.btn_run = toga.Button(
            "RUN MODEL",
            on_press=self.run_model,
            enabled=False,
            style=Pack(flex=1, margin_right=5),
        )
        self.spinner = toga.ActivityIndicator(style=Pack(margin_left=5))

        control_row.add(self.btn_run)
        control_row.add(self.spinner)

        input_box.add(toga.Label("Configuration File:", style=Pack(margin_bottom=2)))
        input_box.add(file_row)
        input_box.add(control_row)

        # 2. Tabs (Logs vs Results)
        self.content_container = toga.OptionContainer(style=Pack(flex=1))

        # Log Tab
        self.log_view = toga.MultilineTextInput(
            readonly=True, style=Pack(flex=1, font_family="monospace")
        )

        # Results Tab (WebView)
        # We start with a valid placeholder to keep WebKit happy
        self.webview = toga.WebView(url="https://beeware.org", style=Pack(flex=1))

        self.content_container.content.append(toga.OptionItem("Logs", self.log_view))
        self.content_container.content.append(toga.OptionItem("Report", self.webview))

        # --- Main Window ---
        main_box = toga.Box(style=Pack(direction=COLUMN))
        main_box.add(input_box)
        main_box.add(self.content_container)

        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = main_box
        self.main_window.show()

    # --- Actions ---

    async def select_file(self, widget):
        try:
            # FIX 2: Modern Dialog API
            fname = await self.main_window.dialog(
                toga.OpenFileDialog(
                    title="Select Temoa Configuration",
                    file_types=["toml", "dat", "txt"],
                )
            )
            if fname:
                self.config_path = fname
                self.path_input.value = fname.name
                self.btn_run.enabled = True
        except ValueError:
            pass

    async def run_model(self, widget):
        if not self.config_path:
            return

        # UI Updates
        self.btn_run.enabled = False
        self.spinner.start()
        self.log_view.value = (
            f"--- Starting Run: {datetime.now().strftime('%H:%M:%S')} ---\n"
        )
        self.content_container.current_tab = 0

        # Offload to thread to prevent GUI freeze
        await asyncio.to_thread(self._execute_temoa_logic)

    def _execute_temoa_logic(self):
        """
        The worker thread that imports and runs Temoa directly.
        """
        # 1. Setup Logging Interception
        root_logger = logging.getLogger()
        gui_handler = TogaLogHandler(self.app, self.log_view)
        root_logger.addHandler(gui_handler)

        logging.getLogger("temoa").setLevel(logging.INFO)

        final_output_path = None
        success = False

        try:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            base_dir = self.config_path.parent / "output_files" / timestamp
            base_dir.mkdir(parents=True, exist_ok=True)

            # Use call_soon_threadsafe here too
            self.app.loop.call_soon_threadsafe(
                self._append_log, f"Output directory: {base_dir}\n"
            )

            config = TemoaConfig.build_config(
                config_file=self.config_path, output_path=base_dir, silent=False
            )

            sequencer = TemoaSequencer(config=config)
            sequencer.start()

            final_output_path = base_dir
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
                    # Pass the file path as the root URL so relative links work
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
