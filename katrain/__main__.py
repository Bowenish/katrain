"""isort:skip_file"""

# first, logging level lower
import os
import sys

os.environ["KCFG_KIVY_LOG_LEVEL"] = os.environ.get("KCFG_KIVY_LOG_LEVEL", "warning")

from kivy.utils import platform as kivy_platform

if kivy_platform == "win":
    from ctypes import windll, c_int64

    if hasattr(windll.user32, "SetProcessDpiAwarenessContext"):
        windll.user32.SetProcessDpiAwarenessContext(c_int64(-4))

import kivy

kivy.require("2.0.0")

# next, icon
from katrain.core.utils import find_package_resource, PATHS
from kivy.config import Config

if kivy_platform == "macosx":
    ICON = find_package_resource("katrain/img/icon.icns")
else:
    ICON = find_package_resource("katrain/img/icon.ico")
Config.set("kivy", "window_icon", ICON)
Config.set("input", "mouse", "mouse,multitouch_on_demand")

# Preload sounds before Window import creates the SDL2 window,
# as SoundLoader.load() deadlocks once the SDL2 window exists.
from katrain.gui.sound import preload_sounds

preload_sounds(PATHS["PACKAGE"] + "/sounds")

# next, certificates on package builds https://github.com/sanderland/katrain/issues/414
if getattr(sys, "frozen", False):
    import ssl

    if ssl.get_default_verify_paths().cafile is None and hasattr(sys, "_MEIPASS"):
        os.environ["SSL_CERT_FILE"] = os.path.join(sys._MEIPASS, "certifi", "cacert.pem")


import re
import signal
import json
import threading
import subprocess
import tempfile
import traceback
from queue import Queue
import urllib3
import webbrowser
import time
import random
import glob

from kivy.base import ExceptionHandler, ExceptionManager
from kivy.app import App
from kivy.core.clipboard import Clipboard
from kivy.lang import Builder
from kivy.resources import resource_add_path
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen
from kivy.core.window import Window
from kivy.uix.widget import Widget
from kivy.resources import resource_find
from kivy.properties import NumericProperty, ObjectProperty, StringProperty
from kivy.clock import Clock
from kivy.metrics import dp
from katrain.core.ai import generate_ai_move

from katrain.core.lang import DEFAULT_LANGUAGE, i18n
from katrain.core.constants import (
    OUTPUT_ERROR,
    OUTPUT_KATAGO_STDERR,
    OUTPUT_INFO,
    OUTPUT_DEBUG,
    OUTPUT_EXTRA_DEBUG,
    MODE_ANALYZE,
    HOMEPAGE,
    VERSION,
    STATUS_ERROR,
    STATUS_INFO,
    PLAYING_NORMAL,
    PLAYER_HUMAN,
    SGF_INTERNAL_COMMENTS_MARKER,
    MODE_PLAY,
    DATA_FOLDER,
    AI_DEFAULT,
)
from katrain.gui.popups import (
    ConfigTeacherPopup,
    ConfigTimerPopup,
    I18NPopup,
    SaveSGFPopup,
    ContributePopup,
    EngineRecoveryPopup,
)
from katrain.gui.sound import play_sound
from katrain.core.base_katrain import KaTrainBase
from katrain.core.remote_engine import make_engine
from katrain.core.contribute_engine import KataGoContributeEngine
from katrain.core.game import Game, IllegalMoveException, KaTrainSGF, BaseGame
from katrain.core.sgf_parser import Move, ParseError
from katrain.gui.popups import ConfigPopup, LoadSGFPopup, NewGamePopup, ConfigAIPopup
from katrain.gui.theme import Theme
from kivymd.app import MDApp

# used in kv
from katrain.gui.kivyutils import *
from katrain.gui.widgets import MoveTree, I18NFileBrowser, SelectionSlider, ScoreGraph  # noqa F401
from katrain.gui.badukpan import AnalysisControls, BadukPanControls, BadukPanWidget  # noqa F401
from katrain.gui.controlspanel import ControlsPanel  # noqa F401


class KaTrainGui(Screen, KaTrainBase):
    """Top level class responsible for tying everything together"""

    zen = NumericProperty(0)
    controls = ObjectProperty(None)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.engine = None
        self.contributing = False

        self.new_game_popup = None
        self.fileselect_popup = None
        self.config_popup = None
        self.ai_settings_popup = None
        self.teacher_settings_popup = None
        self.timer_settings_popup = None
        self.contribute_popup = None

        self.pondering = False
        self.peek_hints = False   # hold Shift -> temporarily show best-move hints (最佳选点)
        self.peek_policy = False  # hold Ctrl -> temporarily show policy (自定义下最佳选点)

        self.animate_contributing = False
        self.message_queue = Queue()

        self.last_key_down = None
        self.last_focus_event = 0

    def log(self, message, level=OUTPUT_INFO):
        super().log(message, level)
        if level == OUTPUT_KATAGO_STDERR and "ERROR" not in self.controls.status.text:
            if self.contributing:
                self.controls.set_status(message, STATUS_INFO)
            elif "starting" in message.lower():
                self.controls.set_status("KataGo engine starting...", STATUS_INFO)
            elif message.startswith("Tuning"):
                self.controls.set_status(
                    "KataGo is tuning settings for first startup, please wait." + message, STATUS_INFO
                )
                return
            elif "ready" in message.lower():
                self.controls.set_status("KataGo engine ready.", STATUS_INFO)
        if (
            level == OUTPUT_ERROR
            or (level == OUTPUT_KATAGO_STDERR and "error" in message.lower() and "tuning" not in message.lower())
        ) and getattr(self, "controls", None):
            self.controls.set_status(f"ERROR: {message}", STATUS_ERROR)

    def handle_animations(self, *_args):
        if self.contributing and self.animate_contributing:
            self.engine.advance_showing_game()
        if (self.contributing and self.animate_contributing) or self.pondering:
            self.board_controls.engine_status_pondering += 5
        else:
            self.board_controls.engine_status_pondering = -1

    @property
    def play_analyze_mode(self):
        return self.play_mode.mode

    def toggle_continuous_analysis(self, quiet=False):
        if self.contributing:
            self.animate_contributing = not self.animate_contributing
        else:
            if self.pondering:
                self.controls.set_status("", STATUS_INFO)
            elif not quiet:  # See #549
                Clock.schedule_once(self.analysis_controls.hints.activate, 0)
            self.pondering = not self.pondering
            self.update_state()

    def toggle_move_num(self):
        # the 'm' key flips the 手数 checkbox in the top bar (single source of truth)
        tog = getattr(self.analysis_controls, "move_num", None)
        if tog is not None:
            tog.trigger_action()

    def _update_progress_bar(self, *_args):
        """Drive the bottom analysis bar from the current node's live visit count."""
        bar = getattr(self, "analysis_progress", None)
        if bar is None:
            return
        node = self.game.current_node if self.game else None
        if node is None:
            bar.progress, bar.text = 0, ""
            return
        done = getattr(node, "root_visits", 0)
        target = max(1, getattr(node, "analysis_visits_requested", 0))
        if target <= 1 or done >= target:        # idle / finished -> empty bar (show only while computing)
            bar.progress, bar.text = 0, ""
        else:
            bar.progress = done / target
            local = "（局部）" if (self.game and getattr(self.game, "region_of_interest", None)) else ""
            bar.text = f"计算中{local}  {done} / {target}  visits  ({bar.progress * 100:.0f}%)"

    def start(self):
        if self.engine:
            return
        self.board_gui.trainer_config = self.config("trainer")
        self.engine = make_engine(self, self.config("engine"))
        threading.Thread(target=self._message_loop_thread, daemon=True).start()
        sgf_args = [
            f
            for f in sys.argv[1:]
            if os.path.isfile(f) and any(f.lower().endswith(ext) for ext in ["sgf", "ngf", "gib"])
        ]
        if sgf_args:
            self.load_sgf_file(sgf_args[0], fast=True, rewind=True)
        else:
            self._do_new_game()

        Clock.schedule_interval(self.handle_animations, 0.1)
        Clock.schedule_interval(self._update_progress_bar, 0.12)
        Window.request_keyboard(None, self, "").bind(on_key_down=self._on_keyboard_down, on_key_up=self._on_keyboard_up)
        Window.bind(on_mouse_down=self._on_mouse_down)

        def set_focus_event(*args):
            self.last_focus_event = time.time()
            if self.peek_hints:   # a held Shift's key-up can be lost on focus change -> clear the peek
                self.peek_hints = False
                self.board_gui.redraw_hover_contents_trigger()
            if self.peek_policy:  # same for a held Ctrl
                self.peek_policy = False
                self.board_gui.redraw_board_contents_trigger()
                self.board_gui.redraw_hover_contents_trigger()

        MDApp.get_running_app().root_window.bind(focus=set_focus_event)

    def restart_engine(self):
        """Rebuild the analysis engine from current config and re-analyze.
        Shared by the engine-settings save and the recovery popup's Retry."""
        self.log("Restarting engine", OUTPUT_INFO)
        self.controls.set_status(i18n._("restarting engine"), STATUS_INFO)
        old_engine = self.engine
        old_engine.shutdown(finish=False)
        new_engine = make_engine(self, self.config("engine"))
        self.engine = new_engine
        self.game.engines = {"B": new_engine, "W": new_engine}
        # old engine was possibly broken, so make sure we redo any failures
        self.game.analyze_all_nodes(analyze_fast=True)
        self.update_state()

    def update_gui(self, cn, redraw_board=False):
        # Handle prisoners and next player display
        prisoners = self.game.prisoner_count
        top, bot = [w.__self__ for w in self.board_controls.circles]  # no weakref
        if self.next_player_info.player == "W":
            top, bot = bot, top
            self.controls.players["W"].active = True
            self.controls.players["B"].active = False
        else:
            self.controls.players["W"].active = False
            self.controls.players["B"].active = True
        self.board_controls.mid_circles_container.clear_widgets()
        self.board_controls.mid_circles_container.add_widget(bot)
        self.board_controls.mid_circles_container.add_widget(top)

        self.controls.players["W"].captures = prisoners["W"]
        self.controls.players["B"].captures = prisoners["B"]

        # update engine status dot
        if not self.engine or not self.engine.check_alive():
            self.board_controls.engine_status_col = Theme.ENGINE_DOWN_COLOR
        elif self.engine.is_idle():
            self.board_controls.engine_status_col = Theme.ENGINE_READY_COLOR
        else:
            self.board_controls.engine_status_col = Theme.ENGINE_BUSY_COLOR
        self.board_controls.queries_remaining = self.engine.queries_remaining()

        # redraw board/stones
        if redraw_board:
            self.board_gui.draw_board()
        self.board_gui.redraw_board_contents_trigger()
        self.controls.update_evaluation()
        self.controls.update_timer(1)
        # update move tree
        self.controls.move_tree.current_node = self.game.current_node

    def update_state(self, redraw_board=False):  # redirect to message queue thread
        self("update_state", redraw_board=redraw_board)

    def _do_update_state(
        self, redraw_board=False
    ):  # is called after every message and on receiving analyses and config changes
        # AI and Trainer/auto-undo handlers
        if not self.game or not self.game.current_node:
            return
        cn = self.game.current_node
        if not self.contributing:
            last_player, next_player = self.players_info[cn.player], self.players_info[cn.next_player]
            if self.play_analyze_mode == MODE_PLAY and self.nav_drawer.state != "open" and self.popup_open is None:
                points_lost = cn.points_lost
                if (
                    last_player.human
                    and cn.analysis_complete
                    and points_lost is not None
                    and points_lost > self.config("trainer/eval_thresholds")[-4]
                ):
                    self.play_mistake_sound(cn)
                teaching_undo = cn.player and last_player.being_taught and cn.parent
                if (
                    teaching_undo
                    and cn.analysis_complete
                    and cn.parent.analysis_complete
                    and not cn.children
                    and not self.game.end_result
                ):
                    self.game.analyze_undo(cn)  # not via message loop
                if (
                    cn.analysis_complete
                    and next_player.ai
                    and not cn.children
                    and not self.game.end_result
                    and not (teaching_undo and cn.auto_undo is None)
                ):  # cn mismatch stops this if undo fired. avoid message loop here or fires repeatedly.
                    self._do_ai_move(cn)
                    Clock.schedule_once(self._play_stone_sound, 0.25)
            if self.engine:
                if self.pondering:
                    self.game.analyze_extra("ponder")
                else:
                    self.engine.stop_pondering()
        Clock.schedule_once(lambda _dt: self.update_gui(cn, redraw_board=redraw_board), -1)  # trigger?

    def update_player(self, bw, **kwargs):
        super().update_player(bw, **kwargs)
        if self.game:
            sgf_name = self.game.root.get_property("P" + bw)
            self.players_info[bw].name = None if not sgf_name or SGF_INTERNAL_COMMENTS_MARKER in sgf_name else sgf_name
        if self.controls:
            self.controls.update_players()
            self.update_state()
        for player_setup_block in PlayerSetupBlock.INSTANCES:
            player_setup_block.update_player_info(bw, self.players_info[bw])

    def set_note(self, note):
        self.game.current_node.note = note

    # The message loop is here to make sure moves happen in the right order, and slow operations don't hang the GUI
    def _message_loop_thread(self):
        while True:
            game, msg, args, kwargs = self.message_queue.get()
            try:
                self.log(f"Message Loop Received {msg}: {args} for Game {game}", OUTPUT_EXTRA_DEBUG)
                if game != self.game.game_id:
                    self.log(
                        f"Message skipped as it is outdated (current game is {self.game.game_id}", OUTPUT_EXTRA_DEBUG
                    )
                    continue
                msg = msg.replace("-", "_")
                if self.contributing:
                    if msg not in [
                        "katago_contribute",
                        "redo",
                        "undo",
                        "update_state",
                        "save_game",
                        "find_mistake",
                    ]:
                        self.controls.set_status(
                            i18n._("gui-locked").format(action=msg), STATUS_INFO, check_level=False
                        )
                        continue
                fn = getattr(self, f"_do_{msg}")
                fn(*args, **kwargs)
                if msg != "update_state":
                    self._do_update_state()
            except Exception as exc:
                self.log(f"Exception in processing message {msg} {args}: {exc}", OUTPUT_ERROR)
                traceback.print_exc()

    def __call__(self, message, *args, **kwargs):
        if self.game:
            if message.endswith("popup"):  # gui code needs to run in main kivy thread.
                if self.contributing and "save" not in message and message != "contribute-popup":
                    self.controls.set_status(
                        i18n._("gui-locked").format(action=message), STATUS_INFO, check_level=False
                    )
                    return
                fn = getattr(self, f"_do_{message.replace('-', '_')}")
                Clock.schedule_once(lambda _dt: fn(*args, **kwargs), -1)
            else:  # game related actions
                self.message_queue.put([self.game.game_id, message, args, kwargs])

    def _do_new_game(self, move_tree=None, analyze_fast=False, sgf_filename=None):
        self.pondering = False
        self._autosave_current_entry()   # save edits to the board we're leaving before detaching
        self.current_entry_id = None     # a fresh game detaches from any loaded card (re-set by library_load)
        mode = self.play_analyze_mode
        if (move_tree is not None and mode == MODE_PLAY) or (move_tree is None and mode == MODE_ANALYZE):
            self.play_mode.switch_ui_mode()  # for new game, go to play, for loaded, analyze
        self.board_gui.animating_pv = None
        self.board_gui.reset_rotation()
        self.engine.on_new_game()  # clear queries
        self.game = Game(
            self,
            self.engine,
            move_tree=move_tree,
            analyze_fast=analyze_fast or not move_tree,
            sgf_filename=sgf_filename,
        )
        for bw, player_info in self.players_info.items():
            player_info.sgf_rank = self.game.root.get_property(bw + "R")
            player_info.calculated_rank = None
            if sgf_filename is not None:  # load game->no ai player
                player_info.player_type = PLAYER_HUMAN
                player_info.player_subtype = PLAYING_NORMAL
            self.update_player(bw, player_type=player_info.player_type, player_subtype=player_info.player_subtype)
        self.controls.graph.initialize_from_game(self.game.root)
        self.update_state(redraw_board=True)

    def _do_katago_contribute(self):
        if self.contributing and not self.engine.server_error and self.engine.katago_process is not None:
            return
        self.contributing = self.animate_contributing = True  # special mode
        if self.play_analyze_mode == MODE_PLAY:  # switch to analysis view
            self.play_mode.switch_ui_mode()
        self.pondering = False
        self.board_gui.animating_pv = None
        for bw, player_info in self.players_info.items():
            self.update_player(bw, player_type=PLAYER_AI, player_subtype=AI_DEFAULT)
        self.engine.shutdown(finish=False)
        self.engine = KataGoContributeEngine(self)
        self.game = BaseGame(self)

    def _do_insert_mode(self, mode="toggle"):
        self.game.set_insert_mode(mode)
        if self.play_analyze_mode != MODE_ANALYZE:
            self.play_mode.switch_ui_mode()

    def _do_ai_move(self, node=None):
        if node is None or self.game.current_node == node:
            mode = self.next_player_info.strategy
            settings = self.config(f"ai/{mode}")
            if settings is not None:
                generate_ai_move(self.game, mode, settings)
            else:
                self.log(f"AI Mode {mode} not found!", OUTPUT_ERROR)

    def _do_undo(self, n_times=1):
        if n_times == "smart":
            n_times = 1
            if self.play_analyze_mode == MODE_PLAY and self.last_player_info.ai and self.next_player_info.human:
                n_times = 2
        self.board_gui.animating_pv = None
        self.game.undo(n_times)

    def _do_reset_analysis(self):
        self.game.reset_current_analysis()

    def _do_resign(self):
        self.game.current_node.end_state = f"{self.game.current_node.player}+R"

    def _do_redo(self, n_times=1):
        self.board_gui.animating_pv = None
        self.game.redo(n_times)

    def _do_rotate(self):
        self.board_gui.rotate_gridpos()

    def _do_find_mistake(self, fn="redo"):
        self.board_gui.animating_pv = None
        getattr(self.game, fn)(9999, stop_on_mistake=self.config("trainer/eval_thresholds")[-4])

    def _do_switch_branch(self, *args):
        self.board_gui.animating_pv = None
        self.controls.move_tree.switch_branch(*args)

    def _play_stone_sound(self, _dt=None):
        play_sound(random.choice(Theme.STONE_SOUNDS))

    def _do_play(self, coords):
        self.board_gui.animating_pv = None
        try:
            old_prisoner_count = self.game.prisoner_count["W"] + self.game.prisoner_count["B"]
            self.game.play(Move(coords, player=self.next_player_info.player))
            if old_prisoner_count < self.game.prisoner_count["W"] + self.game.prisoner_count["B"]:
                play_sound(Theme.CAPTURING_SOUND)
            elif not self.game.current_node.is_pass:
                self._play_stone_sound()

        except IllegalMoveException as e:
            self.controls.set_status(f"Illegal Move: {str(e)}", STATUS_ERROR)

    def _do_analyze_extra(self, mode, **kwargs):
        self.game.analyze_extra(mode, **kwargs)

    def _do_selfplay_setup(self, until_move, target_b_advantage=None):
        self.game.selfplay(int(until_move) if isinstance(until_move, float) else until_move, target_b_advantage)

    def _do_select_box(self):
        self.controls.set_status(i18n._("analysis:region:start"), STATUS_INFO)
        self.board_gui.selecting_region_of_interest = True

    def _do_new_game_popup(self):
        self.controls.timer.paused = True
        if not self.new_game_popup:
            self.new_game_popup = I18NPopup(
                title_key="New Game title", size=[dp(800), dp(900)], content=NewGamePopup(self)
            ).__self__
            self.new_game_popup.content.popup = self.new_game_popup
        self.new_game_popup.open()
        self.new_game_popup.content.update_from_current_game()

    def _do_timer_popup(self):
        self.controls.timer.paused = True
        if not self.timer_settings_popup:
            self.timer_settings_popup = I18NPopup(
                title_key="timer settings", size=[dp(600), dp(500)], content=ConfigTimerPopup(self)
            ).__self__
            self.timer_settings_popup.content.popup = self.timer_settings_popup
        self.timer_settings_popup.open()

    def _do_teacher_popup(self):
        self.controls.timer.paused = True
        if not self.teacher_settings_popup:
            self.teacher_settings_popup = I18NPopup(
                title_key="teacher settings", size=[dp(800), dp(825)], content=ConfigTeacherPopup(self)
            ).__self__
            self.teacher_settings_popup.content.popup = self.teacher_settings_popup
        self.teacher_settings_popup.open()

    def _do_config_popup(self):
        self.controls.timer.paused = True
        if not self.config_popup:
            self.config_popup = I18NPopup(
                title_key="general settings title", size=[dp(1200), dp(950)], content=ConfigPopup(self)
            ).__self__
            self.config_popup.content.popup = self.config_popup
            self.config_popup.title += ": " + self.config_file
        self.config_popup.open()

    def _do_contribute_popup(self):
        if not self.contribute_popup:
            self.contribute_popup = I18NPopup(
                title_key="contribute settings title", size=[dp(1100), dp(800)], content=ContributePopup(self)
            ).__self__
            self.contribute_popup.content.popup = self.contribute_popup
        self.contribute_popup.open()

    def _do_ai_popup(self):
        self.controls.timer.paused = True
        if not self.ai_settings_popup:
            self.ai_settings_popup = I18NPopup(
                title_key="ai settings", size=[dp(750), dp(750)], content=ConfigAIPopup(self)
            ).__self__
            self.ai_settings_popup.content.popup = self.ai_settings_popup
        self.ai_settings_popup.open()

    def _do_engine_recovery_popup(self, error_message, code, engine_type="local"):
        current_open = self.popup_open
        if current_open and isinstance(current_open.content, EngineRecoveryPopup):
            self.log(f"Not opening engine recovery popup with {error_message} as one is already open", OUTPUT_DEBUG)
            return
        popup = I18NPopup(
            title_key="engine recovery",
            size=[dp(600), dp(700)],
            content=EngineRecoveryPopup(self, error_message=error_message, code=code, engine_type=engine_type),
        ).__self__
        popup.content.popup = popup
        popup.open()

    def _do_tsumego_frame(self, ko, margin):
        from katrain.core.tsumego_frame import tsumego_frame_from_katrain_game

        if not self.game.stones:
            return

        black_to_play_p = self.next_player_info.player == "B"
        node, analysis_region = tsumego_frame_from_katrain_game(
            self.game, self.game.komi, black_to_play_p, ko_p=ko, margin=margin
        )
        self.game.set_current_node(node)
        if self.play_mode.mode == MODE_PLAY:
            self.play_mode.switch_ui_mode()  # go to analysis mode
        if analysis_region:
            flattened_region = [
                analysis_region[0][1],
                analysis_region[0][0],
                analysis_region[1][1],
                analysis_region[1][0],
            ]
            self.game.set_region_of_interest(flattened_region)
        if self.game.region_of_interest:
            # region queries never set rootInfo, so a normal query is needed for the
            # best-move points to display (see KaTrainGame.reset_current_analysis)
            node.analyze(self.game.engines[node.next_player])
        node.analyze(self.game.engines[node.next_player], region_of_interest=self.game.region_of_interest)
        self.update_state(redraw_board=True)

    def play_mistake_sound(self, node):
        if self.config("timer/sound") and node.played_mistake_sound is None and Theme.MISTAKE_SOUNDS:
            node.played_mistake_sound = True
            play_sound(random.choice(Theme.MISTAKE_SOUNDS))

    def load_sgf_file(self, file, fast=False, rewind=True):
        if self.contributing:
            return
        try:
            file = os.path.abspath(file)
            move_tree = KaTrainSGF.parse_file(file)
        except (ParseError, FileNotFoundError) as e:
            self.log(i18n._("Failed to load SGF").format(error=e), OUTPUT_ERROR)
            return
        self._do_new_game(move_tree=move_tree, analyze_fast=fast, sgf_filename=file)
        if not rewind:
            self.game.redo(999)

    def _do_analyze_sgf_popup(self):
        if not self.fileselect_popup:
            popup_contents = LoadSGFPopup(self)
            popup_contents.filesel.path = os.path.abspath(os.path.expanduser(self.config("general/sgf_load", ".")))
            self.fileselect_popup = I18NPopup(
                title_key="load sgf title", size=[dp(1200), dp(800)], content=popup_contents
            ).__self__

            def readfile(*_args):
                filename = popup_contents.filesel.filename
                self.fileselect_popup.dismiss()
                path, file = os.path.split(filename)
                if path != self.config("general/sgf_load"):
                    self.log(f"Updating sgf load path default to {path}", OUTPUT_DEBUG)
                    self._config["general"]["sgf_load"] = path
                popup_contents.update_config(False)
                self.save_config("general")
                self.load_sgf_file(filename, popup_contents.fast.active, popup_contents.rewind.active)

            popup_contents.filesel.on_success = readfile
            popup_contents.filesel.on_submit = readfile
        self.fileselect_popup.open()
        self.fileselect_popup.content.filesel.ids.list_view._trigger_update()

    def _do_save_game(self, filename=None):
        filename = filename or self.game.sgf_filename
        if not filename:
            return self("save-game-as-popup")
        try:
            msg = self.game.write_sgf(filename)
            self.log(msg, OUTPUT_INFO)
            self.controls.set_status(msg, STATUS_INFO, check_level=False)
        except Exception as e:
            self.log(f"Failed to save SGF to {filename}: {e}", OUTPUT_ERROR)

    def _do_save_game_as_popup(self):
        popup_contents = SaveSGFPopup(suggested_filename=self.game.generate_filename())
        save_game_popup = I18NPopup(
            title_key="save sgf title", size=[dp(1200), dp(800)], content=popup_contents
        ).__self__

        def readfile(*_args):
            filename = popup_contents.filesel.filename
            if not filename.lower().endswith(".sgf"):
                filename += ".sgf"
            save_game_popup.dismiss()
            path, file = os.path.split(filename.strip())
            if not path:
                path = popup_contents.filesel.path  # whatever dir is shown
            if path != self.config("general/sgf_save"):
                self.log(f"Updating sgf save path default to {path}", OUTPUT_DEBUG)
                self._config["general"]["sgf_save"] = path
                self.save_config("general")
            self._do_save_game(os.path.join(path, file))

        popup_contents.filesel.on_success = readfile
        popup_contents.filesel.on_submit = readfile
        save_game_popup.open()

    def load_sgf_from_clipboard(self):
        clipboard = Clipboard.paste()
        if not clipboard:
            self.controls.set_status("Ctrl-V pressed but clipboard is empty.", STATUS_INFO)
            return

        url_match = re.match(r"(?P<url>https?://[^\s]+)", clipboard)
        if url_match:
            self.log("Recognized url: " + url_match.group(), OUTPUT_INFO)
            http = urllib3.PoolManager()
            response = http.request("GET", url_match.group())
            clipboard = response.data.decode("utf-8")

        try:
            move_tree = KaTrainSGF.parse_sgf(clipboard)
        except Exception as exc:
            self.controls.set_status(
                i18n._("Failed to import from clipboard").format(error=exc, contents=clipboard[:50]), STATUS_INFO
            )
            return
        move_tree.nodes_in_tree[-1].analyze(
            self.engine, analyze_fast=False
        )  # speed up result for looking at end of game
        self._do_new_game(move_tree=move_tree, analyze_fast=True)
        self("redo", 9999)
        self.log("Imported game from clipboard.", OUTPUT_INFO)

    # ---- screenshot board recognition (Ctrl-I) + board library -------------
    @property
    def _screenshot_capture_path(self):
        return os.path.join(tempfile.gettempdir(), "katrain_screenshot.png")  # raw capture; <name>.debug.png is annotated

    def _run_recognizer(self, to_move, capture_path):
        """Run the frame-select + recognise subprocess; return (sgf, err). Blocking - call off-thread.

        It runs in a separate process (its own tkinter overlay, so it does not fight Kivy's
        event loop) and prints the SGF to stdout."""
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # dir that contains the 'katrain' package
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "katrain.core.screenshot_import", "--stdout", "--to-move", to_move,
                 "--debug", "--edit", "--save-capture", capture_path],
                capture_output=True,
                text=True,
                timeout=900,   # includes manual editing time
                cwd=pkg_root,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return (proc.stdout or "").strip(), (proc.stderr or "").strip()
        except Exception as exc:  # noqa
            return "", str(exc)

    def _load_move_tree_from_sgf(self, sgf):
        """Parse an SGF string and load it as a new game (shared by import + library).

        If it is a played-out sequence (手数模式: the root has move children, not just
        AB/AW setup), automatically turn ON move-number display so the 手数 are visible."""
        move_tree = KaTrainSGF.parse_sgf(sgf)
        if getattr(move_tree, "children", None):   # has a first move -> it's a numbered sequence
            tog = getattr(self.analysis_controls, "move_num", None)
            if tog is not None:
                tog.activate()   # auto-show move numbers
        self._do_new_game(move_tree=move_tree, analyze_fast=True)
        self("redo", 9999)
        return move_tree

    def import_board_from_screenshot(self, to_move="B"):
        """Ctrl-I: frame-select a board on screen, recognise it, and load it as a new game."""
        self.controls.set_status("框选屏幕上的棋盘… / drag to select the board on screen", STATUS_INFO)
        capture_path = self._screenshot_capture_path

        def worker():
            sgf, err = self._run_recognizer(to_move, capture_path)
            if err:
                self.log("screenshot import: " + err, OUTPUT_INFO)
            Clock.schedule_once(lambda _dt: self._finish_screenshot_import(sgf), -1)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_screenshot_import(self, sgf):
        if not sgf or not sgf.lstrip().startswith("("):
            self.controls.set_status("截图导入已取消或失败 / screenshot import cancelled or failed", STATUS_INFO)
            return
        try:
            self._load_move_tree_from_sgf(sgf)
        except Exception as exc:  # noqa
            self.controls.set_status(f"截图 SGF 解析失败 / parse failed: {exc}", STATUS_INFO)
            return
        self.controls.set_status("已从截图导入棋形 / imported board from screenshot", STATUS_INFO)
        self.log("Imported board from screenshot.", OUTPUT_INFO)

    def library_load(self, entry):
        """Load a saved library entry onto the board (and surface its lesson switch bar)."""
        sgf = (entry or {}).get("sgf", "")
        if not sgf:
            return
        try:
            self._load_move_tree_from_sgf(sgf)
            self.controls.set_status(f"已载入：{entry.get('name', '棋形')}", STATUS_INFO)
        except Exception as exc:  # noqa
            self.controls.set_status(f"载入失败: {exc}", STATUS_INFO)
        self.library_target = (entry or {}).get("category")   # saves default back to this folder
        self.current_entry_id = (entry or {}).get("id")        # '存入当前棋盘' will update THIS card
        self._refresh_lesson_bar(entry)

    def _refresh_lesson_bar(self, entry):
        """Rebuild the board-switch strip from the loaded entry's folder (its 'lesson')."""
        lb = getattr(self, "lesson_bar", None)
        if lb is None:
            return
        try:
            lb.set_lesson(self, (entry or {}).get("category"), (entry or {}).get("id"))
        except Exception:  # noqa
            pass

    def lesson_switch(self, category, entry_id):
        """Switch to another board within the current lesson (from the switch bar)."""
        from katrain.core.library import default_library

        e = default_library().get(entry_id)
        if e:
            self.library_load(e)

    def _autosave_current_entry(self):
        """Quietly save edits back onto the currently-loaded library card, if any.

        Called when leaving a board (switch / load / new game) and on app close, so positions
        and games are never lost just because '存入当前棋盘' was forgotten. No-op when the live
        board isn't tied to a library card."""
        eid = getattr(self, "current_entry_id", None)
        if not eid or not getattr(self, "game", None):
            return
        from katrain.core.library import default_library

        lib = default_library()
        if not lib.get(eid):
            return
        try:
            sgf = self.game.root.sgf()
            img = None
            try:
                from PIL import Image as _Img

                tmp = os.path.join(tempfile.gettempdir(), "katrain_boardshot.png")
                self.board_gui.export_to_png(tmp)
                img = _Img.open(tmp)
            except Exception:  # noqa
                img = None
            sx, sy = self.game.board_size
            nb = sum(1 for s in self.game.stones if s.player == "B")
            nw = sum(1 for s in self.game.stones if s.player == "W")
            lib.update_entry(eid, sgf, image=img, size=max(sx, sy), nb=nb, nw=nw)
        except Exception:  # noqa
            pass

    def library_new_blank(self, popup=None, size=19):
        """Create an empty board (no screenshot): add it to the library and load it."""
        from katrain.core.library import default_library, DEFAULT_CATEGORY

        sgf = f"(;GM[1]FF[4]CA[UTF-8]SZ[{size}]PL[B])"
        img = None
        try:
            from katrain.core.board_ocr import make_synthetic_board

            img = make_synthetic_board(size=size, black=[], white=[])
        except Exception:  # noqa
            img = None
        cat = DEFAULT_CATEGORY
        cur = getattr(popup, "current", None)
        if cur and cur != "全部":
            cat = cur
        self.library_target = cat   # remember the folder so a later '存入当前棋盘' lands here too
        try:
            entry = default_library().add_entry(sgf, image=img, name="空棋盘", category=cat, size=size, nb=0, nw=0)
        except Exception as exc:  # noqa
            self.controls.set_status(f"新建空棋盘失败 / new blank failed: {exc}", STATUS_INFO)
            return
        try:
            self._load_move_tree_from_sgf(sgf)
        except Exception:  # noqa
            pass
        # set AFTER the load: _do_new_game cleared current_entry_id, so editing+saving updates this card
        self.current_entry_id = entry.get("id")
        self._refresh_lesson_bar(entry)
        if popup is not None:
            try:
                popup.refresh()
            except Exception:  # noqa
                pass
        self.controls.set_status("已新建空棋盘并载入 / blank board created", STATUS_INFO)

    def library_save_current(self, category=None, popup=None):
        """Save the CURRENT board (whatever is on it now) into the library."""
        from katrain.core.library import default_library, DEFAULT_CATEGORY

        if not self.game:
            return
        sgf = self.game.root.sgf()
        # thumbnail: snapshot the live board widget (exact look)
        img = None
        try:
            from PIL import Image as _Img

            tmp = os.path.join(tempfile.gettempdir(), "katrain_boardshot.png")
            self.board_gui.export_to_png(tmp)
            img = _Img.open(tmp)
        except Exception:  # noqa
            img = None
        sx, sy = self.game.board_size
        size = max(sx, sy)
        nb = sum(1 for s in self.game.stones if s.player == "B")
        nw = sum(1 for s in self.game.stones if s.player == "W")
        lib = default_library()
        eid = getattr(self, "current_entry_id", None)
        existing = lib.get(eid) if (eid and category is None) else None
        try:
            if existing:   # a library board is loaded -> save edits back onto the same card
                lib.update_entry(eid, sgf, image=img, size=size, nb=nb, nw=nw)
                cat = existing.get("category", DEFAULT_CATEGORY)
                verb = "已更新"
            else:          # nothing tied to this board yet -> create a new card in the target folder
                cur = getattr(popup, "current", None)
                cat = (category
                       or (cur if (cur and cur != "全部") else None)
                       or getattr(self, "library_target", None)
                       or DEFAULT_CATEGORY)
                name = "棋形 " + time.strftime("%m-%d %H:%M")
                new_entry = lib.add_entry(sgf, image=img, name=name, category=cat, size=size, nb=nb, nw=nw)
                self.current_entry_id = new_entry.get("id")
                verb = "已保存"
        except Exception as exc:  # noqa
            self.controls.set_status(f"保存失败 / save failed: {exc}", STATUS_INFO)
            return
        if popup is not None:
            try:
                popup.refresh()
            except Exception:  # noqa
                pass
        self._refresh_lesson_bar(lib.get(getattr(self, "current_entry_id", None)))
        self.controls.set_status(f"{verb}当前棋盘到库（{cat}）", STATUS_INFO)

    def _native_pick_sgf(self):
        """Native file dialog to pick an SGF (own subprocess; proper OS file browser). Returns path or None."""
        fd, out_path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        script = (
            "import sys, ctypes, tkinter as tk\n"
            "from tkinter import filedialog\n"
            "try: ctypes.windll.shcore.SetProcessDpiAwareness(1)\n"
            "except Exception: pass\n"
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
            "f = filedialog.askopenfilename(title='选择 SGF 棋谱', "
            "filetypes=[('SGF 棋谱', '*.sgf'), ('所有文件', '*.*')])\n"
            "open(sys.argv[1], 'w', encoding='utf-8').write(f or '')\n"
        )
        try:
            subprocess.run([sys.executable, "-c", script, out_path], timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with open(out_path, encoding="utf-8") as fh:
                p = fh.read().strip()
            return p or None
        except Exception:  # noqa
            return None
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass

    def library_import_sgf(self, category=None, popup=None):
        """Pick an SGF file, load it, and save it (with a board snapshot) into the library."""
        def worker():
            path = self._native_pick_sgf()
            if path:
                Clock.schedule_once(lambda _dt: self._do_import_sgf(path, category, popup), -1)

        threading.Thread(target=worker, daemon=True).start()

    def _do_import_sgf(self, path, category, popup):
        try:
            raw = open(path, "rb").read()
            sgf = None
            for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
                try:
                    sgf = raw.decode(enc)
                    break
                except Exception:  # noqa
                    continue
            if sgf is None:
                raise ValueError("无法解码文件编码")
            self._load_move_tree_from_sgf(sgf)
        except Exception as exc:  # noqa
            self.controls.set_status(f"SGF 导入失败 / import failed: {exc}", STATUS_INFO)
            return
        name = os.path.splitext(os.path.basename(path))[0]
        # lock the imported game's metadata NOW (self.game is correct here); the thumbnail is taken
        # a moment later once the board has rendered.
        sx, sy = self.game.board_size
        meta = (max(sx, sy),
                sum(1 for s in self.game.stones if s.player == "B"),
                sum(1 for s in self.game.stones if s.player == "W"))
        Clock.schedule_once(lambda _dt: self._finish_save_to_library(sgf, name, category, popup, meta), 0.4)

    def _finish_save_to_library(self, sgf, name, category, popup, meta=None):
        from katrain.core.library import default_library, DEFAULT_CATEGORY

        img = None
        try:
            from PIL import Image as _Img

            tmp = os.path.join(tempfile.gettempdir(), "katrain_boardshot.png")
            self.board_gui.export_to_png(tmp)
            img = _Img.open(tmp)
        except Exception:  # noqa
            img = None
        if meta is not None:
            size, nb, nw = meta
        else:
            sx, sy = self.game.board_size
            size = max(sx, sy)
            nb = sum(1 for s in self.game.stones if s.player == "B")
            nw = sum(1 for s in self.game.stones if s.player == "W")
        cur = getattr(popup, "current", None)
        cat = (category
               or (cur if (cur and cur != "全部") else None)
               or getattr(self, "library_target", None)
               or DEFAULT_CATEGORY)
        try:
            default_library().add_entry(sgf, image=img, name=name, category=cat, size=size, nb=nb, nw=nw)
        except Exception as exc:  # noqa
            self.controls.set_status(f"入库失败 / save failed: {exc}", STATUS_INFO)
            return
        if popup is not None:
            try:
                popup.refresh()
            except Exception:  # noqa
                pass
        self.controls.set_status(f"已导入 {name} 到库（{cat}）", STATUS_INFO)

    def library_capture(self, category, popup=None, to_move="B"):
        """Frame-select + recognise, save the result into the library under `category`, then load it."""
        self.controls.set_status("框选屏幕上的棋盘… / drag to select the board", STATUS_INFO)
        capture_path = self._screenshot_capture_path

        def worker():
            sgf, err = self._run_recognizer(to_move, capture_path)
            if err:
                self.log("library capture: " + err, OUTPUT_INFO)
            Clock.schedule_once(lambda _dt: self._finish_library_capture(sgf, category, capture_path, popup), -1)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_library_capture(self, sgf, category, capture_path, popup):
        if not sgf or not sgf.lstrip().startswith("("):
            self.controls.set_status("截图入库已取消或失败 / capture cancelled or failed", STATUS_INFO)
            return
        try:
            from katrain.core.library import default_library

            root = KaTrainSGF.parse_sgf(sgf)
            nb = len(root.get_list_property("AB", []))
            nw = len(root.get_list_property("AW", []))
            size = int(root.get_property("SZ", 19) or 19)
            img = None
            try:
                from PIL import Image as _PILImage

                img = _PILImage.open(capture_path)
            except Exception:  # noqa
                img = None
            name = "棋形 " + time.strftime("%m-%d %H:%M")
            default_library().add_entry(sgf, image=img, name=name, category=category, size=size, nb=nb, nw=nw)
        except Exception as exc:  # noqa
            self.controls.set_status(f"入库失败: {exc}", STATUS_INFO)
            return
        try:  # also load it so the user sees what was captured
            self._load_move_tree_from_sgf(sgf)
        except Exception:  # noqa
            pass
        if popup is not None:
            try:
                popup.refresh()
            except Exception:  # noqa
                pass
        self.controls.set_status(f"已入库：{name}（{category}）", STATUS_INFO)

    def on_touch_up(self, touch):
        if touch.is_mouse_scrolling:
            touching_board = self.board_gui.collide_point(*touch.pos) or self.board_controls.collide_point(*touch.pos)
            touching_control_nonscroll = self.controls.collide_point(
                *touch.pos
            ) and not self.controls.notes_panel.collide_point(*touch.pos)
            if self.board_gui.animating_pv is not None and touching_board:
                if touch.button == "scrollup":
                    self.board_gui.adjust_animate_pv_index(1)
                elif touch.button == "scrolldown":
                    self.board_gui.adjust_animate_pv_index(-1)
            elif touching_board or touching_control_nonscroll:  # scroll through moves
                if touch.button == "scrollup":
                    self("redo")
                elif touch.button == "scrolldown":
                    self("undo")
        return super().on_touch_up(touch)

    @property
    def shortcuts(self):
        return {
            k: v
            for ks, v in [
                (Theme.KEY_ANALYSIS_CONTROLS_SHOW_CHILDREN, self.analysis_controls.show_children),
                (Theme.KEY_ANALYSIS_CONTROLS_EVAL, self.analysis_controls.eval),
                (Theme.KEY_ANALYSIS_CONTROLS_HINTS, self.analysis_controls.hints),
                (Theme.KEY_ANALYSIS_CONTROLS_OWNERSHIP, self.analysis_controls.ownership),
                (Theme.KEY_ANALYSIS_CONTROLS_POLICY, self.analysis_controls.policy),
                (Theme.KEY_AI_MOVE, ("ai-move",)),
                (Theme.KEY_ANALYZE_EXTRA_EXTRA, ("analyze-extra", "extra")),
                (Theme.KEY_ANALYZE_EXTRA_EQUALIZE, ("analyze-extra", "equalize")),
                (Theme.KEY_ANALYZE_EXTRA_SWEEP, ("analyze-extra", "sweep")),
                (Theme.KEY_ANALYZE_EXTRA_ALTERNATIVE, ("analyze-extra", "alternative")),
                (Theme.KEY_SELECT_BOX, ("select-box",)),
                (Theme.KEY_RESET_ANALYSIS, ("reset-analysis",)),
                (Theme.KEY_INSERT_MODE, ("insert-mode",)),
                (Theme.KEY_PASS, ("play", None)),
                (Theme.KEY_SELFPLAY_TO_END, ("selfplay-setup", "end", None)),
                (Theme.KEY_NAV_PREV_BRANCH, ("undo", "branch")),
                (Theme.KEY_NAV_BRANCH_DOWN, ("switch-branch", 1)),
                (Theme.KEY_NAV_BRANCH_UP, ("switch-branch", -1)),
                (Theme.KEY_TIMER_POPUP, ("timer-popup",)),
                (Theme.KEY_TEACHER_POPUP, ("teacher-popup",)),
                (Theme.KEY_AI_POPUP, ("ai-popup",)),
                (Theme.KEY_CONFIG_POPUP, ("config-popup",)),
                (Theme.KEY_CONTRIBUTE_POPUP, ("contribute-popup",)),
                (Theme.KEY_STOP_ANALYSIS, ("analyze-extra", "stop")),
            ]
            for k in (ks if isinstance(ks, list) else [ks])
        }

    @property
    def popup_open(self) -> Popup:
        app = App.get_running_app()
        if app:
            first_child = app.root_window.children[0]
            return first_child if isinstance(first_child, Popup) else None

    def _on_keyboard_down(self, _keyboard, keycode, _text, modifiers):
        self.last_key_down = keycode
        ctrl_pressed = "ctrl" in modifiers or ("meta" in modifiers and kivy_platform == "macosx")
        shift_pressed = "shift" in modifiers
        if self.controls.note.focus:
            return  # when making notes, don't allow keyboard shortcuts
        if keycode[1] in ("shift", "rshift"):   # hold Shift -> peek best-move hints
            if not self.peek_hints:
                self.peek_hints = True
                self.board_gui.redraw_hover_contents_trigger()
            return
        if keycode[1] in ("lctrl", "rctrl", "ctrl"):   # hold Ctrl -> peek policy (自定义下最佳选点)
            if not self.peek_policy:
                self.peek_policy = True
                self.board_gui.redraw_board_contents_trigger()  # policy overlay lives on the board canvas
                self.board_gui.redraw_hover_contents_trigger()  # and hides hints, like the policy toggle
            return
        popup = self.popup_open
        if popup:
            if keycode[1] in [
                Theme.KEY_DEEPERANALYSIS_POPUP,
                Theme.KEY_REPORT_POPUP,
                Theme.KEY_TIMER_POPUP,
                Theme.KEY_TEACHER_POPUP,
                Theme.KEY_AI_POPUP,
                Theme.KEY_CONFIG_POPUP,
                Theme.KEY_TSUMEGO_FRAME,
                Theme.KEY_CONTRIBUTE_POPUP,
            ]:  # switch between popups
                popup.dismiss()

                return
            elif keycode[1] in Theme.KEY_SUBMIT_POPUP:
                fn = getattr(popup.content, "on_submit", None)
                if fn:
                    fn()
                return
            else:
                return

        if self.contributing:
            if keycode[1] == Theme.KEY_STOP_CONTRIBUTING:
                self.engine.graceful_shutdown()
                return
            elif keycode[1] in Theme.KEY_PAUSE_CONTRIBUTE:
                self.engine.pause()
                return

        if keycode[1] == Theme.KEY_TOGGLE_CONTINUOUS_ANALYSIS:
            self.toggle_continuous_analysis(quiet=shift_pressed)
        elif keycode[1] == Theme.KEY_TOGGLE_MOVENUM:
            self.toggle_move_num()
        elif keycode[1] == Theme.KEY_TOGGLE_COORDINATES:
            self.board_gui.toggle_coordinates()
        elif keycode[1] in Theme.KEY_PAUSE_TIMER and not ctrl_pressed:
            self.controls.timer.paused = not self.controls.timer.paused
        elif keycode[1] in Theme.KEY_ZEN:
            self.zen = (self.zen + 1) % 3
        elif keycode[1] in Theme.KEY_NAV_PREV:
            self("undo", 1 + shift_pressed * 9 + ctrl_pressed * 9999)
        elif keycode[1] in Theme.KEY_NAV_NEXT:
            self("redo", 1 + shift_pressed * 9 + ctrl_pressed * 9999)
        elif keycode[1] == Theme.KEY_NAV_GAME_START:
            self("undo", 9999)
        elif keycode[1] == Theme.KEY_NAV_GAME_END:
            self("redo", 9999)
        elif keycode[1] == Theme.KEY_MOVE_TREE_MAKE_SELECTED_NODE_MAIN_BRANCH:
            self.controls.move_tree.make_selected_node_main_branch()
        elif keycode[1] == Theme.KEY_NAV_MISTAKE and not ctrl_pressed:
            self("find-mistake", "undo" if shift_pressed else "redo")
        elif keycode[1] == Theme.KEY_MOVE_TREE_DELETE_SELECTED_NODE and ctrl_pressed:
            self.controls.move_tree.delete_selected_node()
        elif keycode[1] == Theme.KEY_MOVE_TREE_TOGGLE_SELECTED_NODE_COLLAPSE and not ctrl_pressed:
            self.controls.move_tree.toggle_selected_node_collapse()
        elif keycode[1] == Theme.KEY_NEW_GAME and ctrl_pressed:
            self("new-game-popup")
        elif keycode[1] == Theme.KEY_LOAD_GAME and ctrl_pressed:
            self("analyze-sgf-popup")
        elif keycode[1] == Theme.KEY_SAVE_GAME and ctrl_pressed:
            self("save-game")
        elif keycode[1] == Theme.KEY_SAVE_GAME_AS and ctrl_pressed:
            self("save-game-as-popup")
        elif keycode[1] == Theme.KEY_COPY and ctrl_pressed:
            Clipboard.copy(self.game.root.sgf())
            self.controls.set_status(i18n._("Copied SGF to clipboard."), STATUS_INFO)
        elif keycode[1] == Theme.KEY_PASTE and ctrl_pressed:
            self.load_sgf_from_clipboard()
        elif keycode[1] == Theme.KEY_IMPORT_SCREENSHOT and ctrl_pressed:
            self.import_board_from_screenshot()
        elif keycode[1] == Theme.KEY_BOARD_LIBRARY and ctrl_pressed:
            self.analysis_controls.dropdown.open_board_library_popup()
        elif keycode[1] == Theme.KEY_NAV_PREV_BRANCH and shift_pressed:
            self("undo", "main-branch")
        elif keycode[1] == Theme.KEY_DEEPERANALYSIS_POPUP:
            self.analysis_controls.dropdown.open_game_analysis_popup()
        elif keycode[1] == Theme.KEY_TSUMEGO_FRAME:
            self.analysis_controls.dropdown.open_tsumego_frame_popup()
        elif keycode[1] == Theme.KEY_REPORT_POPUP:
            self.analysis_controls.dropdown.open_report_popup()
        elif keycode[1] == "f10" and self.debug_level >= OUTPUT_EXTRA_DEBUG:
            import yappi

            yappi.set_clock_type("cpu")
            yappi.start()
            self.log("starting profiler", OUTPUT_ERROR)
        elif keycode[1] == "f11" and self.debug_level >= OUTPUT_EXTRA_DEBUG:
            import time
            import yappi

            stats = yappi.get_func_stats()
            filename = f"callgrind.{int(time.time())}.prof"
            stats.save(filename, type="callgrind")
            self.log(f"wrote profiling results to {filename}", OUTPUT_ERROR)
        elif not ctrl_pressed:
            shortcut = self.shortcuts.get(keycode[1])
            if shortcut is not None:
                if isinstance(shortcut, Widget):
                    shortcut.trigger_action(duration=0)
                else:
                    self(*shortcut)

    def _on_mouse_down(self, _window, _x, _y, button, modifiers):
        # mouse side buttons: mouse4 = back -> undo, mouse5 = forward -> redo
        if button not in ("mouse4", "mouse5"):
            return
        if self.controls.note.focus or self.popup_open or self.contributing:
            return
        ctrl_pressed = "ctrl" in modifiers or ("meta" in modifiers and kivy_platform == "macosx")
        shift_pressed = "shift" in modifiers
        n = 1 + shift_pressed * 9 + ctrl_pressed * 9999
        self("undo" if button == "mouse4" else "redo", n)

    def _on_keyboard_up(self, _keyboard, keycode):
        if keycode[1] in ("shift", "rshift") and self.peek_hints:   # release Shift -> hide peeked hints
            self.peek_hints = False
            self.board_gui.redraw_hover_contents_trigger()
        if keycode[1] in ("lctrl", "rctrl", "ctrl") and self.peek_policy:   # release Ctrl -> hide peeked policy
            self.peek_policy = False
            self.board_gui.redraw_board_contents_trigger()
            self.board_gui.redraw_hover_contents_trigger()
        if keycode[1] in ["alt", "tab"]:
            Clock.schedule_once(lambda *_args: self._single_key_action(keycode), 0.05)

    def _single_key_action(self, keycode):
        if (
            self.controls.note.focus
            or self.popup_open
            or keycode != self.last_key_down
            or time.time() - self.last_focus_event < 0.2  # this is here to prevent alt-tab from firing alt or tab
        ):
            return
        if keycode[1] == "alt":
            self.nav_drawer.set_state("toggle")
        elif keycode[1] == "tab":
            self.play_mode.switch_ui_mode()


class KaTrainApp(MDApp):
    gui = ObjectProperty(None)
    language = StringProperty(DEFAULT_LANGUAGE)

    def __init__(self):
        super().__init__()

    def is_valid_window_position(self, left, top, width, height):
        try:
            from screeninfo import get_monitors

            monitors = get_monitors()
            for monitor in monitors:
                if (
                    left >= monitor.x
                    and left + width <= monitor.x + monitor.width
                    and top >= monitor.y
                    and top + height <= monitor.y + monitor.height
                ):
                    return True
            return False
        except Exception as e:
            return True  # yolo

    def build(self):
        self.icon = ICON  # how you're supposed to set an icon

        self.title = f"KaTrain v{VERSION}"
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Gray"
        self.theme_cls.primary_hue = "200"

        kv_file = find_package_resource("katrain/gui.kv")
        popup_kv_file = find_package_resource("katrain/popups.kv")
        resource_add_path(PATHS["PACKAGE"] + "/fonts")
        resource_add_path(PATHS["PACKAGE"] + "/sounds")
        resource_add_path(PATHS["PACKAGE"] + "/img")
        resource_add_path(os.path.abspath(os.path.expanduser(DATA_FOLDER)))  # prefer resources in .katrain

        theme_files = glob.glob(os.path.join(os.path.expanduser(DATA_FOLDER), "theme*.json"))
        for theme_file in sorted(theme_files):
            try:
                with open(theme_file) as f:
                    theme_overrides = json.load(f)
                for k, v in theme_overrides.items():
                    setattr(Theme, k, v)
                    print(f"[{theme_file}] Found theme override {k} = {v}")
            except Exception as e:  # noqa E722
                print(f"Failed to load theme file {theme_file}: {e}")

        Theme.DEFAULT_FONT = resource_find(Theme.DEFAULT_FONT)
        Builder.load_file(kv_file)

        Window.bind(on_request_close=self.on_request_close)
        Window.bind(on_dropfile=lambda win, file: self.gui.load_sgf_file(file.decode("utf8")))
        self.gui = KaTrainGui()
        Builder.load_file(popup_kv_file)

        win_left = win_top = win_size = None
        if self.gui.config("ui_state/restoresize", True):
            win_size = self.gui.config("ui_state/size", [])
            win_left = self.gui.config("ui_state/left", None)
            win_top = self.gui.config("ui_state/top", None)
        if not win_size:
            window_scale_fac = 1
            try:
                from screeninfo import get_monitors

                for m in get_monitors():
                    window_scale_fac = min(window_scale_fac, (m.height - 100) / 1000, (m.width - 100) / 1300)
            except Exception as e:
                window_scale_fac = 0.85
            win_size = [1300 * window_scale_fac, 1000 * window_scale_fac]
        self.gui.log(f"Setting window size to {win_size} and position to {[win_left, win_top]}", OUTPUT_DEBUG)
        Window.size = (win_size[0], win_size[1])
        if (
            win_left is not None
            and win_top is not None
            and self.is_valid_window_position(win_left, win_top, win_size[0], win_size[1])
        ):
            Window.left = win_left
            Window.top = win_top

        return self.gui

    def on_language(self, _instance, language):
        self.gui.log(f"Switching language to {language}", OUTPUT_INFO)
        i18n.switch_lang(language)
        self.gui._config["general"]["lang"] = language
        self.gui.save_config()
        if self.gui.game:
            self.gui.update_state()
            self.gui.controls.set_status("", STATUS_INFO)

    def webbrowser(self, site_key):
        websites = {
            "homepage": HOMEPAGE + "#manual",
            "support": HOMEPAGE + "#support",
            "contribute:signup": "http://katagotraining.org/accounts/signup/",
            "engine:help": HOMEPAGE + "/blob/master/ENGINE.md",
        }
        if site_key in websites:
            webbrowser.open(websites[site_key])

    def on_start(self):
        self.language = self.gui.config("general/lang")
        self.gui.start()

    def on_request_close(self, *_args, source=None):
        if source == "keyboard":
            return True  # do not close on esc
        if getattr(self, "gui", None):
            self.gui._autosave_current_entry()   # don't lose an unsaved library board on exit
            self.gui.play_mode.save_ui_state()
            self.gui._config["ui_state"]["size"] = list(Window._size)
            self.gui._config["ui_state"]["top"] = Window.top
            self.gui._config["ui_state"]["left"] = Window.left
            self.gui.save_config("ui_state")
            if self.gui.engine:
                self.gui.engine.shutdown(finish=None)

    def signal_handler(self, _signal, _frame):
        if self.gui.debug_level >= OUTPUT_DEBUG:
            print("TRACEBACKS")
            for threadId, stack in sys._current_frames().items():
                print(f"\n# ThreadID: {threadId}")
                for filename, lineno, name, line in traceback.extract_stack(stack):
                    print(f"\tFile: {filename}, line {lineno}, in {name}")
                    if line:
                        print(f"\t\t{line.strip()}")
        self.stop()


def run_app():
    class CrashHandler(ExceptionHandler):
        def handle_exception(self, inst):
            ex_type, ex, tb = sys.exc_info()
            trace = "".join(traceback.format_tb(tb))
            app = MDApp.get_running_app()

            if app and app.gui:
                app.gui.log(
                    f"Exception {inst.__class__.__name__}: {', '.join(repr(a) for a in inst.args)}\n{trace}",
                    OUTPUT_ERROR,
                )
            else:
                print(f"Exception {inst.__class__}: {inst.args}\n{trace}")
            return ExceptionManager.PASS

    ExceptionManager.add_handler(CrashHandler())
    app = KaTrainApp()
    signal.signal(signal.SIGINT, app.signal_handler)
    app.run()


if __name__ == "__main__":
    run_app()
