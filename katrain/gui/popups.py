import glob
import json
import os
import re
import stat
import subprocess
import threading
import time
from typing import Any, Dict, List, Tuple, Union
from zipfile import ZipFile

import urllib3
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.properties import BooleanProperty, ListProperty, NumericProperty, ObjectProperty, StringProperty
from kivy.uix.anchorlayout import AnchorLayout
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.spinner import Spinner, SpinnerOption
from kivy.uix.widget import Widget
from kivy.utils import platform
from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.selectioncontrol import MDCheckbox
from kivymd.uix.textfield import MDTextField

from katrain.core.ai import ai_rank_estimation, game_report
from katrain.core.engine import resolve_engine_backend
from katrain.core.constants import (
    AI_CONFIG_DEFAULT,
    AI_DEFAULT,
    AI_KEY_PROPERTIES,
    AI_OPTION_VALUES,
    AI_STRATEGIES_RECOMMENDED_ORDER,
    DATA_FOLDER,
    OUTPUT_DEBUG,
    OUTPUT_ERROR,
    OUTPUT_INFO,
    SGF_INTERNAL_COMMENTS_MARKER,
    STATUS_INFO,
    PLAYER_HUMAN,
    ADDITIONAL_MOVE_ORDER,
)
from katrain.core.lang import i18n, rank_label
from katrain.core.library import default_library, DEFAULT_CATEGORY, SEP, norm_path, parent_path, leaf_name
from katrain.core.sgf_parser import Move
from katrain.core.utils import PATHS, find_package_resource, evaluation_class
from katrain.gui.kivyutils import (
    BackgroundMixin,
    I18NSpinner,
    BackgroundLabel,
    TableHeaderLabel,
    TableCellLabel,
    TableStatLabel,
    PlayerInfo,
    SizedRectangleButton,
    AutoSizedRectangleButton,
    BGBoxLayout,
)
from katrain.gui.theme import Theme
from katrain.gui.widgets.progress_loader import ProgressLoader


class I18NPopup(Popup):
    title_key = StringProperty("")
    font_name = StringProperty(Theme.DEFAULT_FONT)

    def __init__(self, size=None, **kwargs):
        if size:  # do not exceed window size
            app = MDApp.get_running_app()
            size[0] = min(app.gui.width, size[0])
            size[1] = min(app.gui.height, size[1])
        super().__init__(size=size, **kwargs)
        self.bind(on_dismiss=Clock.schedule_once(lambda _dt: MDApp.get_running_app().gui.update_state(), 1))


class LabelledTextInput(MDTextField):
    input_property = StringProperty("")
    multiline = BooleanProperty(False)

    @property
    def input_value(self):
        return self.text

    @property
    def raw_input_value(self):
        return self.text


class LabelledPathInput(LabelledTextInput):
    check_path = BooleanProperty(True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        Clock.schedule_once(self.check_error, 0)

    def check_error(self, _dt=None):
        file = find_package_resource(self.input_value, silent_errors=True)
        self.error = self.check_path and not (file and os.path.exists(file))

    def on_text(self, widget, text):
        self.check_error()
        return super().on_text(widget, text)

    @property
    def input_value(self):
        return self.text.strip().replace("\n", " ").replace("\r", " ")


class LabelledCheckBox(MDCheckbox):
    input_property = StringProperty("")

    def __init__(self, text=None, **kwargs):
        if text is not None:
            kwargs["active"] = text.lower() == "true"
        super().__init__(**kwargs)

    @property
    def input_value(self):
        return bool(self.active)

    def raw_input_value(self):
        return self.active


class LabelledSpinner(I18NSpinner):
    input_property = StringProperty("")

    @property
    def input_value(self):
        return self.selected[1]  # ref value

    def raw_input_value(self):
        return self.text


class LabelledFloatInput(LabelledTextInput):
    input_filter = ObjectProperty("float")

    @property
    def input_value(self):
        return float(self.text or "0.0")


class LabelledIntInput(LabelledTextInput):
    input_filter = ObjectProperty("int")

    @property
    def input_value(self):
        return int(self.text or "0")


class LabelledSelectionSlider(BoxLayout):
    input_property = StringProperty("")
    values = ListProperty([(0, "")])  # (value:numeric,label:string) pairs
    key_option = BooleanProperty(False)

    def set_value(self, v):
        self.slider.set_value(v)
        self.textbox.text = str(v)

    @property
    def input_value(self):
        if self.textbox.text:
            return float(self.textbox.text)
        return self.slider.values[self.slider.index][0]

    @property
    def raw_input_value(self):
        return self.textbox.text


class InputParseError(Exception):
    pass


class QuickConfigGui(MDBoxLayout):
    def __init__(self, katrain):
        super().__init__()
        self.katrain = katrain
        self.popup = None
        Clock.schedule_once(self.build_and_set_properties, 0)

    def collect_properties(self, widget) -> Dict:
        if isinstance(
            widget, (LabelledTextInput, LabelledSpinner, LabelledCheckBox, LabelledSelectionSlider)
        ) and getattr(widget, "input_property", None):
            try:
                ret = {widget.input_property: widget.input_value}
            except Exception as e:  # TODO : on widget?
                raise InputParseError(
                    f"Could not parse value '{widget.raw_input_value}' for {widget.input_property} ({widget.__class__.__name__}): {e}"
                )
        else:
            ret = {}
        for c in widget.children:
            for k, v in self.collect_properties(c).items():
                ret[k] = v
        return ret

    def get_setting(self, key) -> Union[Tuple[Any, Dict, str], Tuple[Any, List, int]]:
        keys = key.split("/")
        config = self.katrain._config
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]

        if "::" in keys[-1]:
            array_key, ix = keys[-1].split("::")
            ix = int(ix)
            array = config[array_key]
            return array[ix], array, ix
        else:
            if keys[-1] not in config:
                config[keys[-1]] = ""
                self.katrain.log(
                    f"Configuration setting {repr(key)} was missing, created it, but this likely indicates a broken config file.",
                    OUTPUT_ERROR,
                )
            return config[keys[-1]], config, keys[-1]

    def build_and_set_properties(self, *_args):
        return self._set_properties_subtree(self)

    def _set_properties_subtree(self, widget):
        if isinstance(
            widget, (LabelledTextInput, LabelledSpinner, LabelledCheckBox, LabelledSelectionSlider)
        ) and getattr(widget, "input_property", None):
            value = self.get_setting(widget.input_property)[0]
            if isinstance(widget, LabelledCheckBox):
                widget.active = value is True
            elif isinstance(widget, LabelledSelectionSlider):
                widget.set_value(value)
            elif isinstance(widget, LabelledSpinner):
                selected = 0
                try:
                    selected = widget.value_refs.index(value)
                except:  # noqa: E722
                    pass
                widget.text = widget.values[selected]
            else:
                widget.text = str(value)
        for c in widget.children:
            self._set_properties_subtree(c)

    def update_config(self, save_to_file=True, close_popup=True):
        updated = set()
        for multikey, value in self.collect_properties(self).items():
            old_value, conf, key = self.get_setting(multikey)
            if value != old_value:
                self.katrain.log(f"Updating setting {multikey} = {value}", OUTPUT_DEBUG)
                conf[key] = value  # reference straight back to katrain._config - may be array or dict
                updated.add(multikey)
        if save_to_file:
            self.katrain.save_config()
        if self.popup and close_popup:
            self.popup.dismiss()
        return updated


class ConfigTimerPopup(QuickConfigGui):
    def update_config(self, save_to_file=True, close_popup=True):
        super().update_config(save_to_file=save_to_file, close_popup=close_popup)
        for p in self.katrain.players_info.values():
            p.periods_used = 0
        self.katrain.controls.timer.paused = True
        self.katrain.game.current_node.time_used = 0
        self.katrain.game.main_time_used = 0
        self.katrain.update_state()


class NewGamePopup(QuickConfigGui):
    mode = StringProperty("newgame")

    def __init__(self, katrain):
        super().__init__(katrain)
        for bw, info in katrain.players_info.items():
            self.player_setup.update_player_info(bw, info)

        self.rules_spinner.value_refs = [name for abbr, name in katrain.engine.RULESETS_ABBR]
        self.bind(mode=self.update_playername)
        Clock.schedule_once(self.update_from_current_game, 0.1)

    def normalized_rules(self):
        rules = self.katrain.game.root.get_property("RU", "japanese").strip().lower()
        for abbr, name in self.katrain.engine.RULESETS_ABBR:
            if abbr == rules or name == rules:
                return name

    def update_playerinfo(self, *args):
        for bw, player_setup in self.player_setup.players.items():
            name = self.player_name[bw].text
            if name:
                self.katrain.game.root.set_property("P" + bw, name)
            else:
                self.katrain.game.root.clear_property("P" + bw)
            self.katrain.update_player(bw, **player_setup.player_type_dump)

    def update_playername(self, *args):
        for bw in "BW":
            name = self.katrain.game.root.get_property("P" + bw, None)
            if name and SGF_INTERNAL_COMMENTS_MARKER not in name:
                self.player_name[bw].text = name if self.mode == "editgame" else ""

    def update_from_current_game(self, *args):  # set rules and komi
        rules = self.normalized_rules()
        self.km.text = str(self.katrain.game.root.komi)
        if rules is not None:
            self.rules_spinner.select_key(rules.strip())

    def update_config(self, save_to_file=True, close_popup=True):
        super().update_config(save_to_file=save_to_file, close_popup=close_popup)
        props = self.collect_properties(self)
        self.katrain.log(f"Mode: {self.mode}, settings: {self.katrain.config('game')}", OUTPUT_DEBUG)
        self.update_playerinfo()  # type
        if self.mode == "newgame":
            if self.restart.active:
                self.katrain.log("Restarting Engine", OUTPUT_DEBUG)
                self.katrain.engine.restart()
            self.katrain._do_new_game()
        elif self.mode == "editgame":
            root = self.katrain.game.root
            changed = False
            for k, currentval, newval in [
                ("RU", self.normalized_rules(), props["game/rules"]),
                ("KM", root.komi, props["game/komi"]),
            ]:
                if currentval != newval:
                    changed = True
                    self.katrain.log(
                        f"Property {k} changed from {currentval} to {newval}, triggering re-analysis of entire game.",
                        OUTPUT_INFO,
                    )
                    self.katrain.game.root.set_property(k, newval)
            if changed:
                self.katrain.engine.on_new_game()
                self.katrain.game.analyze_all_nodes(analyze_fast=True)
        else:  # setup position
            self.katrain._do_new_game()
            self.katrain("selfplay-setup", props["game/setup_move"], props["game/setup_advantage"])
        self.update_playerinfo()  # name


def wrap_anchor(widget):
    anchor = AnchorLayout()
    anchor.add_widget(widget)
    return anchor


class ConfigTeacherPopup(QuickConfigGui):
    def __init__(self, katrain):
        super().__init__(katrain)
        MDApp.get_running_app().bind(language=self.build_and_set_properties)

    def add_option_widgets(self, widgets):
        for widget in widgets:
            self.options_grid.add_widget(wrap_anchor(widget))

    def build_and_set_properties(self, *_args):
        theme = self.katrain.config("trainer/theme")
        undos = self.katrain.config("trainer/num_undo_prompts")
        thresholds = self.katrain.config("trainer/eval_thresholds")
        savesgfs = self.katrain.config("trainer/save_feedback")
        show_dots = self.katrain.config("trainer/show_dots")

        self.themes_spinner.value_refs = list(Theme.EVAL_COLORS.keys())
        self.options_grid.clear_widgets()

        for k in ["dot color", "point loss threshold", "num undos", "show dots", "save dots"]:
            self.options_grid.add_widget(DescriptionLabel(text=i18n._(k), font_name=i18n.font_name, font_size=dp(17)))

        for i, color, threshold, undo, show_dot, savesgf in list(
            zip(range(len(thresholds)), Theme.EVAL_COLORS[theme], thresholds, undos, show_dots, savesgfs)
        )[::-1]:
            self.add_option_widgets(
                [
                    BackgroundMixin(background_color=color, size_hint=[0.9, 0.9]),
                    LabelledFloatInput(text=str(threshold), input_property=f"trainer/eval_thresholds::{i}"),
                    LabelledFloatInput(text=str(undo), input_property=f"trainer/num_undo_prompts::{i}"),
                    LabelledCheckBox(text=str(show_dot), input_property=f"trainer/show_dots::{i}"),
                    LabelledCheckBox(text=str(savesgf), input_property=f"trainer/save_feedback::{i}"),
                ]
            )
        super().build_and_set_properties()

    def update_config(self, save_to_file=True, close_popup=True):
        super().update_config(save_to_file=save_to_file, close_popup=close_popup)
        self.build_and_set_properties()


class DescriptionLabel(Label):
    pass


class ConfigAIPopup(QuickConfigGui):
    max_options = NumericProperty(6)

    def __init__(self, katrain):
        super().__init__(katrain)
        self.ai_select.value_refs = AI_STRATEGIES_RECOMMENDED_ORDER
        selected_strategies = {p.strategy for p in katrain.players_info.values()}
        config_strategy = list((selected_strategies - {AI_DEFAULT}) or {AI_CONFIG_DEFAULT})[0]
        self.ai_select.select_key(config_strategy)
        self.build_ai_options()
        self.ai_select.bind(text=self.build_ai_options)

    def estimate_rank_from_options(self, *_args):
        strategy = self.ai_select.selected[1]
        try:
            options = self.collect_properties(self)  # [strategy]
        except InputParseError:
            self.estimated_rank_label.text = "??"
            return
        prefix = f"ai/{strategy}/"
        options = {k[len(prefix) :]: v for k, v in options.items() if k.startswith(prefix)}
        dan_rank = ai_rank_estimation(strategy, options)
        self.estimated_rank_label.text = rank_label(dan_rank)

    def build_ai_options(self, *_args):
        strategy = self.ai_select.selected[1]
        mode_settings = self.katrain.config(f"ai/{strategy}")
        self.options_grid.clear_widgets()
        self.help_label.text = i18n._(strategy.replace("ai:", "aihelp:"))
        for k, v in sorted(mode_settings.items(), key=lambda kv: (kv[0] not in AI_KEY_PROPERTIES, kv[0])):
            self.options_grid.add_widget(DescriptionLabel(text=k, size_hint_x=0.275))
            if k in AI_OPTION_VALUES:
                values = AI_OPTION_VALUES[k]
                if values == "bool":
                    widget = LabelledCheckBox(input_property=f"ai/{strategy}/{k}")
                    widget.active = v
                    widget.bind(active=self.estimate_rank_from_options)
                else:
                    if isinstance(values[0], Tuple):  # with descriptions, possibly language-specific
                        fixed_values = [(v, re.sub(r"\[(.*?)]", lambda m: i18n._(m[1]), l)) for v, l in values]
                    else:  # just numbers
                        fixed_values = [(v, str(v)) for v in values]
                    widget = LabelledSelectionSlider(
                        values=fixed_values, input_property=f"ai/{strategy}/{k}", key_option=(k in AI_KEY_PROPERTIES)
                    )
                    widget.set_value(v)
                    widget.textbox.bind(text=self.estimate_rank_from_options)
                self.options_grid.add_widget(wrap_anchor(widget))
            else:
                self.options_grid.add_widget(
                    wrap_anchor(LabelledFloatInput(text=str(v), input_property=f"ai/{strategy}/{k}"))
                )
        for _ in range((self.max_options - len(mode_settings)) * 2):
            self.options_grid.add_widget(Label(size_hint_x=None))
        Clock.schedule_once(self.estimate_rank_from_options)

    def update_config(self, save_to_file=True, close_popup=True):
        super().update_config(save_to_file=save_to_file, close_popup=close_popup)
        self.katrain.update_calculated_ranks()
        Clock.schedule_once(self.katrain.controls.update_players, 0)


class EngineRecoveryPopup(QuickConfigGui):
    error_message = StringProperty("")
    code = ObjectProperty(None)
    engine_type = StringProperty("local")
    recovery_message = StringProperty("")

    def __init__(self, katrain, error_message, code, engine_type="local"):
        super().__init__(katrain)
        self.error_message = str(error_message)
        self.code = code
        self.engine_type = engine_type or "local"
        self.recovery_message = self._build_message()

    def _build_message(self):
        settings_link = "[color=#CCCC11][u][ref=engine_settings]" + i18n._("menu:settings") + "[/ref][/u][/color]"
        help_link = "[color=#CCCC11][u][ref=engine_help]" + i18n._("link_here") + "[/ref][/u][/color]"
        if self.engine_type == "remote":
            opening_key = "remote engine disconnected popup opening message"
            suggestion = i18n._("remote engine check url suggestion").format(link=settings_link)
        else:
            opening_key = "engine died popup opening message"
            suggestion = i18n._("change engine suggestion").format(link=settings_link)
        opening = i18n._(opening_key).format(code=self.code, error_message=self.error_message)
        help_text = i18n._("go to engine help page").format(link=help_link)
        return opening + "\n\n" + suggestion + "\n\n" + help_text

    def retry(self):
        """Rebuild the engine from current config and re-analyze. For a
        remote engine this reconnects; for a local one it respawns the
        subprocess. Recovers a transient failure without changing settings."""
        if self.popup:
            self.popup.dismiss()
        Clock.schedule_once(lambda _dt: self.katrain.restart_engine(), 0)


class BaseConfigPopup(QuickConfigGui):
    MODEL_ENDPOINTS = {
        "Latest distributed model": "https://katagotraining.org/api/networks/newest_training/",
        "Strongest distributed model": "https://katagotraining.org/api/networks/get_strongest/",
    }
    MODELS = {
        "old 15 block model": "https://github.com/lightvector/KataGo/releases/download/v1.3.2/g170e-b15c192-s1672170752-d466197061.txt.gz",
        "Human-like model": "https://github.com/lightvector/KataGo/releases/download/v1.15.0/b18c384nbt-humanv0.bin.gz",
    }
    MODEL_DESC = {
        "Fat 40 block model": "https://d3dndmfyhecmj0.cloudfront.net/g170/neuralnets/g170e-b40c384x2-s2348692992-d1229892979.zip",
        "Recommended 18b model": "https://media.katagotraining.org/uploaded/networks/models/kata1/kata1-b18c384nbt-s9996604416-d4316597426.bin.gz",
        "old 20 block model": "https://github.com/lightvector/KataGo/releases/download/v1.4.5/g170e-b20c256x2-s5303129600-d1228401921.bin.gz",
        "old 30 block model": "https://github.com/lightvector/KataGo/releases/download/v1.4.5/g170-b30c320x2-s4824661760-d1229536699.bin.gz",
        "old 40 block model": "https://github.com/lightvector/KataGo/releases/download/v1.4.5/g170-b40c256x2-s5095420928-d1229425124.bin.gz",
    }

    KATAGOS = {
        "win": {
            "OpenCL v1.16.5": "https://github.com/lightvector/KataGo/releases/download/v1.16.5/katago-v1.16.5-opencl-windows-x64.zip",
            "Eigen AVX2 (Modern CPUs) v1.16.5": "https://github.com/lightvector/KataGo/releases/download/v1.16.5/katago-v1.16.5-eigenavx2-windows-x64.zip",
            "Eigen (CPU, Non-optimized) v1.16.5": "https://github.com/lightvector/KataGo/releases/download/v1.16.5/katago-v1.16.5-eigen-windows-x64.zip",
            "OpenCL v1.16.5 (bigger boards)": "https://github.com/lightvector/KataGo/releases/download/v1.16.5/katago-v1.16.5-opencl-windows-x64+bs50.zip",
        },
        "linux": {
            "OpenCL v1.16.5": "https://github.com/lightvector/KataGo/releases/download/v1.16.5/katago-v1.16.5-opencl-linux-x64.zip",
            "Eigen AVX2 (Modern CPUs) v1.16.5": "https://github.com/lightvector/KataGo/releases/download/v1.16.5/katago-v1.16.5-eigenavx2-linux-x64.zip",
            "Eigen (CPU, Non-optimized) v1.16.5": "https://github.com/lightvector/KataGo/releases/download/v1.16.5/katago-v1.16.5-eigen-linux-x64.zip",
            "OpenCL v1.16.5 (bigger boards)": "https://github.com/lightvector/KataGo/releases/download/v1.16.5/katago-v1.16.5-opencl-linux-x64+bs50.zip",
        },
        "just-descriptions": {},
    }

    def __init__(self, katrain):
        super().__init__(katrain)
        self.paths = [
            self.katrain.config("engine/model"),
            self.katrain.config("engine/humanlike_model"),
            "katrain/models",
            DATA_FOLDER,
        ]
        self.katago_paths = [self.katrain.config("engine/katago"), DATA_FOLDER]
        self.last_clicked_download_models = 0

    def check_models(self, *args):
        all_models = [self.MODELS, self.MODEL_DESC, self.katrain.config("dist_models", {})]

        def extract_model_file(model):
            try:
                return re.match(r".*/([^/]+)", model)[1].replace(".zip", ".bin.gz")
            except (TypeError, IndexError):
                return None

        def find_description(path):
            file = os.path.split(path)[1]
            file_to_desc = {extract_model_file(model): desc for mods in all_models for desc, model in mods.items()}
            if file in file_to_desc:
                return f"{file_to_desc[file]}  -  {path}"
            else:
                return path

        done = set()
        model_files = []
        humanlike_model_files = []
        distributed_training_models = os.path.expanduser(os.path.join(DATA_FOLDER, "katago_contribute/kata1/models"))
        for path in self.paths + [self.model_path.text, self.humanlike_model_path.text, distributed_training_models]:
            path = (path or "").rstrip("/\\")
            if path.startswith("katrain"):
                path = path.replace("katrain", PATHS["PACKAGE"].rstrip("/\\"), 1)
            path = os.path.expanduser(path)
            if not os.path.isdir(path):
                path, _file = os.path.split(path)
            slashpath = path.replace("\\", "/")
            if slashpath in done or not os.path.isdir(path):
                continue
            done.add(slashpath)
            files = [
                f.replace("/", os.path.sep).replace(PATHS["PACKAGE"], "katrain")
                for ftype in ["*.bin.gz", "*.txt.gz"]
                for f in glob.glob(slashpath + "/" + ftype)
                if ".tmp." not in f
            ]
            if files and path not in self.paths:
                self.paths.append(path)  # persistent on paths with models found
            model_files += files
            for file in files:
                if "human" in file:
                    humanlike_model_files.append(file)

        # no description to bottom
        model_files = sorted(
            [(find_description(path), path) for path in model_files],
            key=lambda descpath: ("Recommended" not in descpath[0], "  -  " not in descpath[0], descpath[0]),
        )
        models_available_msg = i18n._("models available").format(num=len(model_files))
        self.model_files.values = [models_available_msg] + [desc for desc, path in model_files]
        self.model_files.value_keys = [""] + [path for desc, path in model_files]
        self.model_files.text = models_available_msg

        humanlike_model_files = sorted(
            [(find_description(path), path) for path in humanlike_model_files],
            key=lambda descpath: ("Recommended" not in descpath[0], "  -  " not in descpath[0], descpath[0]),
        )
        humanlike_models_available_msg = i18n._("models available").format(num=len(humanlike_model_files))
        self.humanlike_model_files.values = [humanlike_models_available_msg] + [
            desc for desc, path in humanlike_model_files
        ]
        self.humanlike_model_files.value_keys = [""] + [path for desc, path in humanlike_model_files]
        self.humanlike_model_files.text = humanlike_models_available_msg

    def check_katas(self, *args):
        def find_description(path):
            file = os.path.split(path)[1].replace(".exe", "")
            file_to_desc = {
                re.match(r".*/([^/]+)", kg)[1].replace(".zip", ""): desc
                for _, kgs in self.KATAGOS.items()
                for desc, kg in kgs.items()
            }
            if file in file_to_desc:
                return f"{file_to_desc[file]}  -  {path}"
            else:
                return path

        done = set()
        kata_files = []
        for path in self.katago_paths + [self.katago_path.text]:
            path = path.rstrip("/\\")
            if path.startswith("katrain"):
                path = path.replace("katrain", PATHS["PACKAGE"].rstrip("/\\"), 1)
            path = os.path.expanduser(path)
            if not os.path.isdir(path):
                path, _file = os.path.split(path)
            slashpath = path.replace("\\", "/")
            if slashpath in done or not os.path.isdir(path):
                continue
            done.add(slashpath)
            files = [
                f.replace("/", os.path.sep).replace(PATHS["PACKAGE"], "katrain")
                for ftype in ["katago*"]
                for f in glob.glob(slashpath + "/" + ftype)
                if os.path.isfile(f) and not f.endswith(".zip")
            ]
            if files and path not in self.paths:
                self.paths.append(path)  # persistent on paths with models found
            kata_files += files

        kata_files = sorted(
            [(path, find_description(path)) for path in kata_files],
            key=lambda f: ("bs29" in f[0]) * 0.1 - (f[0] != f[1]),
        )
        katas_available_msg = i18n._("katago binaries available").format(num=len(kata_files))
        self.katago_files.values = [katas_available_msg, i18n._("default katago option")] + [
            desc for path, desc in kata_files
        ]
        self.katago_files.value_keys = ["", ""] + [path for path, desc in kata_files]
        self.katago_files.text = katas_available_msg

    def download_models(self, *_largs):
        if time.time() - self.last_clicked_download_models > 5:
            self.last_clicked_download_models = time.time()
            threading.Thread(target=self._download_models, daemon=True).start()

    def _download_models(self):
        def download_complete(req, tmp_path, path, model):
            try:
                os.rename(tmp_path, path)
                self.katrain.log(f"Download of {model} complete -> {path}", OUTPUT_INFO)
            except Exception as e:
                self.katrain.log(f"Download of {model} complete, but could not move file: {e}", OUTPUT_ERROR)
            self.check_models()

        for c in self.download_progress_box.children:
            if isinstance(c, ProgressLoader) and c.request:
                c.request.cancel()
        Clock.schedule_once(lambda _dt: self.download_progress_box.clear_widgets(), -1)  # main thread
        downloading = False

        dist_models = {k: v for k, v in self.katrain.config("dist_models", {}).items() if k in self.MODEL_ENDPOINTS}

        for name, url in self.MODEL_ENDPOINTS.items():
            try:
                http = urllib3.PoolManager()
                response = http.request("GET", url)
                if response.status != 200:
                    raise Exception(
                        f"Request to {url} returned code {response.status} != 200: {response.data.decode()}"
                    )
                dist_models[name] = json.loads(response.data.decode("utf-8"))["model_file"]
            except Exception as e:
                self.katrain.log(f"Failed to retrieve info for model: {e}", OUTPUT_INFO)
        self.katrain._config["dist_models"] = dist_models
        self.katrain.save_config(key="dist_models")

        for name, url in {**self.MODELS, **dist_models}.items():
            filename = os.path.split(url)[1]
            if not any(
                os.path.split(f)[1] == filename for f in self.model_files.values + self.humanlike_model_files.values
            ):
                savepath = os.path.expanduser(os.path.join(DATA_FOLDER, filename))
                savepath_tmp = savepath + ".part"
                self.katrain.log(f"Downloading {name} from {url} to {savepath_tmp}", OUTPUT_INFO)
                Clock.schedule_once(
                    lambda _dt, _savepath=savepath, _savepath_tmp=savepath_tmp, _url=url, _name=name: ProgressLoader(
                        self.download_progress_box,
                        download_url=_url,
                        path_to_file=_savepath_tmp,
                        downloading_text=f"Downloading {_name}: " + "{}",
                        label_downloading_text=f"Starting download for {_name}",
                        download_complete=lambda req, tmp=_savepath_tmp, path=_savepath, model=_name: download_complete(
                            req, tmp, path, model
                        ),
                        download_redirected=lambda req, mname=_name: self.katrain.log(
                            f"Download {mname} redirected {req.resp_headers}", OUTPUT_DEBUG
                        ),
                        download_error=lambda req, error, mname=_name: self.katrain.log(
                            f"Download of {mname} failed or cancelled ({error})", OUTPUT_ERROR
                        ),
                    ),
                    0,
                )  # main thread
                downloading = True
        if not downloading:
            Clock.schedule_once(
                lambda _dt: self.download_progress_box.add_widget(
                    Label(text=i18n._("All models downloaded"), font_name=i18n.font_name, text_size=(None, dp(50)))
                ),
                0,
            )  # main thread

    def download_katas(self, *_largs):
        def unzipped_name(zipfile):
            if platform == "win":
                return zipfile.replace(".zip", ".exe")
            else:
                return zipfile.replace(".zip", "")

        def download_complete(req, tmp_path, path, binary):
            try:
                if tmp_path.endswith(".zip"):
                    with ZipFile(tmp_path, "r") as zipObj:
                        exes = [f for f in zipObj.namelist() if f.startswith("katago")]
                        if len(exes) != 1:
                            raise FileNotFoundError(
                                f"Zip file {tmp_path} does not contain exactly 1 file starting with 'katago' (contents: {zipObj.namelist()})"
                            )
                        with open(path, "wb") as fout:
                            fout.write(zipObj.read(exes[0]))
                            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP)
                        for f in zipObj.namelist():
                            if f.lower().endswith("dll"):
                                try:
                                    with open(os.path.join(os.path.split(path)[0], f), "wb") as fout:
                                        fout.write(zipObj.read(f))
                                except:  # already there? no problem
                                    pass
                    os.remove(tmp_path)
                else:
                    os.rename(tmp_path, path)
                self.katrain.log(f"Download of katago binary {binary} complete -> {path}", OUTPUT_INFO)
            except Exception as e:
                self.katrain.log(
                    f"Download of katago binary {binary} complete, but could not move file: {e}", OUTPUT_ERROR
                )
            self.check_katas()

        for c in self.katago_download_progress_box.children:
            if isinstance(c, ProgressLoader) and c.request:
                c.request.cancel()
        self.katago_download_progress_box.clear_widgets()
        downloading = False
        for name, url in self.KATAGOS.get(platform, {}).items():
            filename = os.path.split(url)[1]
            exe_name = unzipped_name(filename)
            if not any(os.path.split(f)[1] == exe_name for f in self.katago_files.values):
                savepath_tmp = os.path.expanduser(os.path.join(DATA_FOLDER, filename))
                exe_path_name = os.path.expanduser(os.path.join(DATA_FOLDER, exe_name))
                self.katrain.log(f"Downloading binary {name} from {url} to {savepath_tmp}", OUTPUT_INFO)
                ProgressLoader(
                    root_instance=self.katago_download_progress_box,
                    download_url=url,
                    path_to_file=savepath_tmp,
                    downloading_text=f"Downloading {name}: " + "{}",
                    label_downloading_text=f"Starting download for {name}",
                    download_complete=lambda req, tmp=savepath_tmp, path=exe_path_name, model=name: download_complete(
                        req, tmp, path, model
                    ),
                    download_redirected=lambda req, mname=name: self.katrain.log(
                        f"Download {mname} redirected {req.resp_headers}", OUTPUT_DEBUG
                    ),
                    download_error=lambda req, error, mname=name: self.katrain.log(
                        f"Download of {mname} failed or cancelled ({error})", OUTPUT_ERROR
                    ),
                )
                downloading = True
        if not downloading:
            if not self.KATAGOS.get(platform):
                self.katago_download_progress_box.add_widget(
                    Label(text=f"No binaries available for platform {platform}", text_size=(None, dp(50)))
                )
            else:
                self.katago_download_progress_box.add_widget(
                    Label(text=i18n._("All binaries downloaded"), font_name=i18n.font_name, text_size=(None, dp(50)))
                )


class ConfigPopup(BaseConfigPopup):
    ENGINE_TAB_BUTTONS = {"local": "local_tab_button", "remote": "remote_tab_button", "custom": "custom_tab_button"}

    def __init__(self, katrain):
        super().__init__(katrain)
        Clock.schedule_once(self.check_katas)
        Clock.schedule_once(self.select_engine_tab)
        MDApp.get_running_app().bind(language=self.check_models)
        MDApp.get_running_app().bind(language=self.check_katas)

    def select_engine_tab(self, *_args):
        # The active tab is authoritative for which engine is used; pick it based on the current config.
        backend = resolve_engine_backend(self.katrain.config("engine"))
        self.engine_sm.current = backend
        getattr(self, self.ENGINE_TAB_BUTTONS[backend]).state = "down"

    def update_config(self, save_to_file=True, close_popup=True):
        old_backend = self.katrain.config("engine/backend", "")
        backend = self.engine_sm.current
        self.katrain._config["engine"]["backend"] = backend
        updated = super().update_config(save_to_file=save_to_file, close_popup=close_popup)
        if backend != old_backend:
            updated.add("engine/backend")
        self.katrain.debug_level = self.katrain.config("general/debug_level", OUTPUT_INFO)

        ignore = {"max_visits", "fast_visits", "max_time", "enable_ownership", "wide_root_noise"}
        detected_restart = [key for key in updated if "engine" in key and not any(ig in key for ig in ignore)]
        if detected_restart:

            def restart_engine(_dt):
                self.katrain.log(f"Restarting Engine after {detected_restart} settings change")
                self.katrain.restart_engine()

            Clock.schedule_once(restart_engine, 0)


class ContributePopup(BaseConfigPopup):
    def __init__(self, katrain):
        super().__init__(katrain)
        MDApp.get_running_app().bind(language=self.check_katas)
        Clock.schedule_once(self.check_katas)

    def start_contributing(self):
        self.update_config(True, close_popup=False)
        self.error.text = ""
        log_settings = {**self.katrain.config("contribute"), "password": "***"}
        self.katrain.log(f"Updating contribution settings {log_settings}", OUTPUT_DEBUG)
        if not self.katrain.config("contribute/username") or not self.katrain.config("contribute/password"):
            self.error.text = "Please enter your username and password for katagotraining.org"
        else:
            self.popup.dismiss()
            self.katrain("katago-contribute")


class LoadSGFPopup(BaseConfigPopup):
    def __init__(self, katrain):
        super().__init__(katrain)
        app = MDApp.get_running_app()
        self.filesel.favorites = [
            (os.path.abspath(app.gui.config("general/sgf_load")), "Last Load Dir"),
            (os.path.abspath(app.gui.config("general/sgf_save")), "Last Save Dir"),
        ]
        self.filesel.path = os.path.abspath(os.path.expanduser(app.gui.config("general/sgf_load")))
        self.filesel.select_string = "Load File"

    def on_submit(self):
        self.filesel.button_clicked()


class SaveSGFPopup(BoxLayout):
    def __init__(self, suggested_filename, **kwargs):
        super().__init__(**kwargs)
        self.suggested_filename = suggested_filename
        app = MDApp.get_running_app()
        self.filesel.favorites = [
            (os.path.abspath(app.gui.config("general/sgf_load")), "Last Load Dir"),
            (os.path.abspath(app.gui.config("general/sgf_save")), "Last Save Dir"),
        ]
        save_path = os.path.expanduser(MDApp.get_running_app().gui.config("general/sgf_save") or ".")

        def set_suggested(_widget, path):
            self.filesel.ids.file_text.text = os.path.join(path, self.suggested_filename)

        self.filesel.ids.list_view.bind(path=set_suggested)
        self.filesel.path = os.path.abspath(save_path)
        self.filesel.select_string = "Save File"

    def on_submit(self):
        self.filesel.button_clicked()


class ReAnalyzeGamePopup(BoxLayout):
    popup = ObjectProperty(None)

    def on_checkbox_active(self, checkbox, value):
        self.start_move.opacity = 1.0 if value else 0.3
        self.end_move.opacity = 1.0 if value else 0.3
        self.start_move.disabled = not value
        self.end_move.disabled = not value

    def __init__(self, katrain, **kwargs):
        super().__init__(**kwargs)

        self.katrain = katrain
        self.move_range.bind(active=self.on_checkbox_active)

        self.start_move.disabled = True
        self.end_move.disabled = True
        self.start_move.opacity = 0.3
        self.end_move.opacity = 0.3

        self.start_move.text = str(katrain.game.current_node.depth)

    def on_submit(self):
        self.button.trigger_action(duration=0)


class TsumegoFramePopup(BoxLayout):
    katrain = ObjectProperty(None)
    popup = ObjectProperty(None)

    def on_submit(self):
        self.button.trigger_action(duration=0)


class GameReportPopup(BoxLayout):
    def __init__(self, katrain, **kwargs):
        super().__init__(**kwargs)
        self.katrain = katrain
        self.depth_filter = None
        Clock.schedule_once(self._refresh, 0)

    def set_depth_filter(self, filter):
        self.depth_filter = filter
        Clock.schedule_once(self._refresh, 0)

    def _refresh(self, _dt=0):
        game = self.katrain.game
        thresholds = self.katrain.config("trainer/eval_thresholds")

        sum_stats, histogram, player_ptloss = game_report(game, depth_filter=self.depth_filter, thresholds=thresholds)
        labels = [f"≥ {pt}" if pt > 0 else f"< {thresholds[-2]}" for pt in thresholds]

        table = GridLayout(cols=3, rows=6 + len(thresholds))
        colors = [
            [cp * 0.75 for cp in col[:3]] + [1] for col in Theme.EVAL_COLORS[self.katrain.config("trainer/theme")]
        ]

        table.add_widget(TableHeaderLabel(text="", background_color=Theme.BACKGROUND_COLOR))
        table.add_widget(TableHeaderLabel(text=i18n._("header:keystats"), background_color=Theme.BACKGROUND_COLOR))
        table.add_widget(TableHeaderLabel(text="", background_color=Theme.BACKGROUND_COLOR))

        for i, (label, fmt, stat, scale, more_is_better) in enumerate(
            [
                ("accuracy", "{:.1f}", "accuracy", 100, True),
                ("meanpointloss", "{:.2f}", "mean_ptloss", 5, False),
                ("aitopmove", "{:.1%}", "ai_top_move", 1, True),
                ("aitop5", "{:.1%}", "ai_top5_move", 1, True),
            ]
        ):
            statcell = {
                bw: TableStatLabel(
                    text=fmt.format(sum_stats[bw][stat]) if stat in sum_stats[bw] else "",
                    side=side,
                    value=sum_stats[bw].get(stat, 0),
                    scale=scale,
                    bar_color=(
                        Theme.STAT_BETTER_COLOR
                        if (sum_stats[bw].get(stat, 0) < sum_stats[Move.opponent_player(bw)].get(stat, 0))
                        ^ more_is_better
                        else Theme.STAT_WORSE_COLOR
                    ),
                    background_color=Theme.BOX_BACKGROUND_COLOR,
                )
                for (bw, side) in zip("BW", ["left", "right"])
            }
            table.add_widget(statcell["B"])
            table.add_widget(TableCellLabel(text=i18n._(f"stat:{label}"), background_color=Theme.BOX_BACKGROUND_COLOR))
            table.add_widget(statcell["W"])

        table.add_widget(TableHeaderLabel(text=i18n._("header:num moves"), background_color=Theme.BACKGROUND_COLOR))
        table.add_widget(TableHeaderLabel(text=i18n._("stats:pointslost"), background_color=Theme.BACKGROUND_COLOR))
        table.add_widget(TableHeaderLabel(text=i18n._("header:num moves"), background_color=Theme.BACKGROUND_COLOR))

        for i, (col, label, pt) in enumerate(zip(colors[::-1], labels[::-1], thresholds[::-1])):
            statcell = {
                bw: TableStatLabel(
                    text=str(histogram[i][bw]),
                    side=side,
                    value=histogram[i][bw],
                    scale=len(player_ptloss[bw]) + 1e-6,
                    bar_color=col,
                    background_color=Theme.BOX_BACKGROUND_COLOR,
                )
                for (bw, side) in zip("BW", ["left", "right"])
            }
            table.add_widget(statcell["B"])
            table.add_widget(TableCellLabel(text=label, background_color=col))
            table.add_widget(statcell["W"])

        self.stats.clear_widgets()
        self.stats.add_widget(table)

        for bw, player_info in self.katrain.players_info.items():
            self.player_infos[bw].player_type = player_info.player_type
            self.player_infos[bw].captures = ""  # ;)
            self.player_infos[bw].player_subtype = player_info.player_subtype
            self.player_infos[bw].name = player_info.name
            self.player_infos[bw].rank = (
                player_info.sgf_rank
                if player_info.player_type == PLAYER_HUMAN
                else rank_label(player_info.calculated_rank)
            )

        # if not done analyzing, check again in 1s
        if not self.katrain.engine.is_idle():
            Clock.schedule_once(self._refresh, 1)


class _CJKSpinnerOption(SpinnerOption):
    """Spinner dropdown item that renders CJK (default Roboto font cannot)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("font_name", Theme.DEFAULT_FONT)
        super().__init__(**kwargs)


def _native_text_prompt(title, initial=""):
    """Ask for a line of text using a NATIVE OS dialog (tkinter), which has full IME support.

    Kivy's SDL2 text field doesn't pop the IME candidate window, so Chinese can't be typed
    there. We run a tiny tkinter askstring in a subprocess (its own Tk loop, no clash with
    Kivy) and read the result from a temp file as UTF-8. Returns the text, or None if cancelled.
    Source- or frozen-safe via helper_cmd (see katrain.core.subtasks).
    """
    import tempfile

    from katrain.core.subtasks import helper_cmd, helper_cwd

    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        subprocess.run(helper_cmd("ask_text", path, title, initial or ""), cwd=helper_cwd(),
                       timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with open(path, encoding="utf-8") as f:
            v = f.read()
        return None if v == "\x00" else v
    except Exception:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class BoardLibraryPopup(BoxLayout):
    """A browsable, categorised collection of imported board shapes.

    Top bar : category selector + new/delete category + 'capture into library'.
    Body    : a scrolling grid of thumbnails - click one to load it on the board.

    Built in Python (like GameReportPopup) so there is no extra .kv to maintain.
    All user-visible widgets use Theme.DEFAULT_FONT so Chinese text renders.
    """

    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", spacing=dp(6), padding=dp(8), **kwargs)
        self.katrain = None
        self.popup = None
        self.lib = default_library()
        self.current = ""   # current folder path; "" = root (顶层)

        # hover/press feedback: one Window.mouse_pos listener drives all buttons (auto-unbinds on dismiss)
        self._hovers = []                 # [ [btn, normal, hover, press], ... ] for the dynamic content
        self._static_hovers = []          # the always-present top-bar buttons
        Window.bind(mouse_pos=self._on_hover_move)

        # top bar: [面包屑导航 ............] [↑上级] [＋新建文件夹] [存入当前棋盘]
        # (截图入库 / 空白棋盘 / 导入SGF all live together on the first 'create' card)
        bar = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(46), spacing=dp(6), padding=[dp(2), 0])
        self.crumb = BoxLayout(orientation="horizontal", spacing=dp(2))
        bar.add_widget(self.crumb)

        def barbtn(text, cb):
            b = AutoSizedRectangleButton(text=text, font_name=Theme.DEFAULT_FONT, size_hint=(None, 1))
            b.background_radius = dp(8)
            b.outline_color = Theme.BUTTON_BORDER_COLOR
            b.bind(on_release=lambda *_a: cb())
            self._fx(b, Theme.BOX_BACKGROUND_COLOR, Theme.LIGHTER_BACKGROUND_COLOR, [0.34, 0.45, 0.58, 1], blank=False)
            return b

        self.up_btn = barbtn("↑ 上级", self._go_up)
        bar.add_widget(self.up_btn)
        bar.add_widget(barbtn("＋ 新建文件夹", self._new_folder))
        bar.add_widget(barbtn("存入当前棋盘", self._save_current))
        self.add_widget(bar)

        # body: left action sidebar (create actions) + right scrolling list (folders & boards)
        body = BoxLayout(orientation="horizontal", spacing=dp(8))
        sidebar = BoxLayout(orientation="vertical", size_hint_x=None, width=dp(150), spacing=dp(8))
        sidebar.add_widget(self._make_new_card())   # 空白棋盘 / 截图入库 / 导入SGF
        sidebar.add_widget(Widget())                # push the action card to the top
        body.add_widget(sidebar)

        self.scroll = ScrollView()
        # no TOP padding: the first row (region masonry) lines up with the sidebar card's top
        self.grid = GridLayout(cols=1, spacing=dp(6), padding=[dp(4), 0, dp(4), dp(4)], size_hint_y=None)
        self.grid.bind(minimum_height=self.grid.setter("height"))
        self.scroll.add_widget(self.grid)
        body.add_widget(self.scroll)
        self.add_widget(body)

        # bar + sidebar buttons persist across refreshes; crumb + grid buttons are rebuilt each time
        self._static_hovers = list(self._hovers)

        Clock.schedule_once(lambda _dt: self.refresh(), 0)

    # ----------------------------------------------------------- helpers --
    @staticmethod
    def _btn(text, cb, **kw):
        # SizedRectangleButton defaults to size_hint=(None,None) size 100x100 (and its inner
        # label font = 0.6*height -> huge). Always give it a layout-driven size.
        kw.setdefault("size_hint", (1, 1))
        b = SizedRectangleButton(text=text, font_name=Theme.DEFAULT_FONT, **kw)
        b.background_color = Theme.BOX_BACKGROUND_COLOR
        b.background_radius = dp(8)
        b.bind(on_release=lambda *_a: cb())
        return b

    @staticmethod
    def _lbl(text, **kw):
        kw.setdefault("font_name", Theme.DEFAULT_FONT)
        kw.setdefault("color", Theme.TEXT_COLOR)
        return Label(text=text, **kw)

    # ----------------------------------------------------- hover / press fx --
    def _fx(self, btn, normal, hover, press, blank=True):
        """Give a button hover-highlight + press feedback by swapping its background_color.

        `blank=True` blanks a plain kivy Button's default texture so the colour shows; the custom
        RectangleButtons (bar) draw their own background, so pass blank=False for them."""
        if blank and hasattr(btn, "background_normal"):
            btn.background_normal = ""
            btn.background_down = ""
        btn._fx = [list(normal), list(hover), list(press)]
        btn._hv = False
        btn.background_color = list(normal)
        self._hovers.append(btn)
        btn.bind(state=lambda b, *_a: self._apply_fx(b))
        return btn

    @staticmethod
    def _apply_fx(btn):
        normal, hover, press = btn._fx
        btn.background_color = press if btn.state == "down" else (hover if btn._hv else normal)

    def _on_hover_move(self, _win, pos):
        if not self.get_root_window():            # popup dismissed -> stop listening (auto-cleanup)
            Window.unbind(mouse_pos=self._on_hover_move)
            return
        for btn in self._hovers:
            try:
                inside = btn.get_root_window() is not None and btn.collide_point(*btn.to_widget(*pos))
            except Exception:  # noqa
                inside = False
            if inside != btn._hv:
                btn._hv = inside
                self._apply_fx(btn)

    def on_touch_down(self, touch):
        # mouse side button (X1 'back') -> up one level, like a file browser
        if "button" in touch.profile and touch.button == "mouse4" and self.collide_point(*touch.pos):
            self._go_up()
            return True
        return super().on_touch_down(touch)

    # --------------------------------------------------------- navigation --
    def _enter(self, path):
        self.current = norm_path(path)
        self.refresh()

    def _go_up(self):
        self._enter(parent_path(self.current))

    def _rebuild_crumb(self):
        """Rebuild the clickable breadcrumb: 根 › jinjin › 第一节课 ."""
        self.crumb.clear_widgets()
        segs = [("根", "")]
        acc = ""
        for p in [s for s in self.current.split(SEP) if s]:
            acc = (acc + SEP + p) if acc else p
            segs.append((p, acc))
        for i, (label, path) in enumerate(segs):
            if i > 0:
                self.crumb.add_widget(self._lbl("›", size_hint_x=None, width=dp(14)))
            b = Button(text=label, font_name=Theme.DEFAULT_FONT, size_hint_x=None, width=dp(40),
                       color=Theme.TEXT_COLOR)
            b.bind(texture_size=lambda w, ts: setattr(w, "width", ts[0] + dp(10)))
            b.bind(on_release=lambda _w, pth=path: self._enter(pth))
            self._fx(b, [0, 0, 0, 0], [1, 1, 1, 0.12], [1, 1, 1, 0.20])
            self.crumb.add_widget(b)
        self.crumb.add_widget(Widget())   # spacer pushes the bar buttons to the right

    # ------------------------------------------------------------ render --
    def refresh(self, *_args):
        if getattr(self, "_refreshing", False):
            return
        self._refreshing = True
        try:
            self._hovers = list(self._static_hovers)   # drop hover-refs to the about-to-be-cleared widgets
            # if the current folder was deleted out from under us, climb to the nearest survivor
            folders = self.lib._all_folders()
            while self.current and self.current not in folders:
                self.current = parent_path(self.current)
            self._rebuild_crumb()
            self.up_btn.disabled = (self.current == "")
            self.grid.clear_widgets()
            children = self.lib.child_folders(self.current)
            if children:   # region blocks in a tight 2-column masonry (no ragged row gaps)
                self.grid.add_widget(self._masonry(children))
            entries = self.lib.entries(self.current)          # boards directly in this folder -> list rows
            for e in entries:
                self.grid.add_widget(self._entry_row(e))
            if not children and not entries:
                self.grid.add_widget(self._lbl("（这个文件夹还是空的，用左侧按钮新建棋盘）",
                                               size_hint_y=None, height=dp(40)))
        finally:
            self._refreshing = False

    def _card_base(self):
        card = BGBoxLayout(orientation="vertical", size_hint_y=None, height=dp(172), spacing=dp(3), padding=dp(5))
        card.background_color = Theme.BOX_BACKGROUND_COLOR
        card.background_radius = dp(7)
        return card

    def _make_new_card(self):
        """The 'create' tile: three stacked actions — screenshot capture / blank board / import SGF."""
        card = self._card_base()
        card.background_color = Theme.LIGHTER_BACKGROUND_COLOR
        card.outline_color = Theme.BUTTON_BORDER_COLOR
        card.outline_width = dp(1.2)

        def section(text, cb):
            b = Button(text=text, font_name=Theme.DEFAULT_FONT, font_size=dp(16), size_hint_y=1,
                       color=Theme.TEXT_COLOR)
            b.bind(on_release=lambda *_a: cb())
            self._fx(b, [0, 0, 0, 0], [1, 1, 1, 0.10], [1, 1, 1, 0.18])
            return b

        def divider():
            d = BackgroundLabel(text="", size_hint_y=None, height=dp(1))
            d.background_color = Theme.BUTTON_BORDER_COLOR
            return d

        card.add_widget(section("＋  空白棋盘", self._new_blank))
        card.add_widget(divider())
        card.add_widget(section("▣  截图入库", self._capture))
        card.add_widget(divider())
        card.add_widget(section("↓  导入SGF", self._import_sgf))
        return card

    def _row_base(self):
        row = BGBoxLayout(orientation="horizontal", size_hint_y=None, height=dp(56),
                          spacing=dp(8), padding=[dp(8), dp(4)])
        row.background_color = Theme.BOX_BACKGROUND_COLOR
        row.background_radius = dp(8)
        return row

    def _row_menu_btn(self, cb):
        b = Button(text="⋯", font_name=Theme.DEFAULT_FONT, font_size=dp(22), size_hint_x=None, width=dp(40),
                   color=Theme.TEXT_COLOR)
        b.bind(on_release=lambda *_a: cb())
        self._fx(b, [0, 0, 0, 0], [1, 1, 1, 0.14], [1, 1, 1, 0.22])
        return b

    def _entry_row(self, e):
        """One board as a list row: thumbnail + name + meta ........ ⋯ menu."""
        row = self._row_base()
        thumb = self.lib.thumb_path(e)
        if os.path.exists(thumb):
            icon = Button(background_normal=thumb, background_down=thumb, border=(0, 0, 0, 0),
                          size_hint_x=None, width=dp(48))
        else:
            icon = Button(text="棋", font_name=Theme.DEFAULT_FONT, size_hint_x=None, width=dp(48),
                          background_normal="", background_color=Theme.LIGHTER_BACKGROUND_COLOR, color=Theme.TEXT_COLOR)
        icon.bind(on_release=lambda *_a: self.load_entry(e))
        row.add_widget(icon)
        label = Button(
            text=f"{e.get('name', '棋形')}      [color=999999]SZ{e.get('size', 19)}  ●{e.get('nb', 0)} ○{e.get('nw', 0)}[/color]",
            markup=True, font_name=Theme.DEFAULT_FONT, font_size=dp(16), halign="left", valign="middle",
            shorten=True, shorten_from="right", color=Theme.TEXT_COLOR)
        label.bind(size=lambda w, *_a: setattr(w, "text_size", (w.width, w.height)))
        label.bind(on_release=lambda *_a: self.load_entry(e))
        self._fx(label, [0, 0, 0, 0], [1, 1, 1, 0.08], [1, 1, 1, 0.16])
        row.add_widget(label)
        row.add_widget(self._row_menu_btn(lambda: self._card_menu(e)))
        return row

    def _est_block_height(self, path):
        """Rough rendered height of a region block, for balancing the masonry columns."""
        subs = self.lib.child_folders(path)
        rows = len(subs) if subs else 1
        return dp(16 + 34 + 1 + 6) + rows * dp(33)   # padding + header + divider + spacing + item rows

    def _masonry(self, children):
        """Pack region blocks into 2 tight columns (Pinterest-style): each block goes into the
        currently-shorter column, so there are no ragged row gaps from uneven block heights."""
        container = BoxLayout(orientation="horizontal", size_hint_y=None, spacing=dp(8))
        # pos_hint top:1 pins each column's TOP to the container top, so a shorter column lines up
        # at the top instead of being bottom-aligned (the default for size_hint_y=None in a row).
        cols = [BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(8), pos_hint={"top": 1}),
                BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(8), pos_hint={"top": 1})]
        for c in cols:
            c.bind(minimum_height=c.setter("height"))
            container.add_widget(c)
        est = [0.0, 0.0]
        for f in children:
            i = 0 if est[0] <= est[1] else 1
            cols[i].add_widget(self._region_block(f))
            est[i] += self._est_block_height(f) + dp(8)

        def _sync(*_a):
            container.height = max(cols[0].height, cols[1].height)
        cols[0].bind(height=_sync)
        cols[1].bind(height=_sync)
        _sync()
        return container

    def _region_block(self, path):
        """A category shown as a block: header (the region) + its sub-folders listed inside, clickable.

        A region with no sub-folders (only boards) shows a board count; click the header to open it."""
        block = BGBoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(3), padding=dp(8))
        block.background_color = Theme.LIGHTER_BACKGROUND_COLOR
        block.background_radius = dp(8)
        block.outline_color = Theme.BUTTON_BORDER_COLOR
        block.outline_width = dp(1.2)
        block.bind(minimum_height=block.setter("height"))

        # header: region name (click -> open it) + ⋯ folder menu (rename / move / delete)
        header = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(34), spacing=dp(2))
        title = Button(text=leaf_name(path), font_name=Theme.DEFAULT_FONT, font_size=dp(17),
                       halign="left", valign="middle", shorten=True, shorten_from="right", color=Theme.TEXT_COLOR)
        title.bind(size=lambda w, *_a: setattr(w, "text_size", (w.width, w.height)))
        title.bind(on_release=lambda *_a: self._enter(path))
        self._fx(title, [0, 0, 0, 0], [1, 1, 1, 0.10], [1, 1, 1, 0.18])
        header.add_widget(title)
        header.add_widget(self._row_menu_btn(lambda: self._folder_menu(path)))
        block.add_widget(header)

        div = BackgroundLabel(text="", size_hint_y=None, height=dp(1))
        div.background_color = Theme.BUTTON_BORDER_COLOR
        block.add_widget(div)

        subs = self.lib.child_folders(path)
        if subs:
            for sf in subs:
                n, ns = len(self.lib.entries(sf)), len(self.lib.child_folders(sf))
                meta = f"{n}盘" + (f" · {ns}夹" if ns else "")
                r = Button(text=f"›  {leaf_name(sf)}      [color=999999]{meta}[/color]",
                           markup=True, font_name=Theme.DEFAULT_FONT, font_size=dp(15),
                           halign="left", valign="middle", shorten=True, shorten_from="right",
                           size_hint_y=None, height=dp(30), color=Theme.TEXT_COLOR)
                r.bind(size=lambda w, *_a: setattr(w, "text_size", (w.width, w.height)))
                r.bind(on_release=lambda _w, p=sf: self._enter(p))
                self._fx(r, [0, 0, 0, 0], [1, 1, 1, 0.10], [1, 1, 1, 0.18])
                block.add_widget(r)
        else:
            n = len(self.lib.entries(path))
            lbl = self._lbl(f"（{n} 个棋盘，点标题进入）" if n else "（空）",
                            size_hint_y=None, height=dp(30), halign="left", valign="middle",
                            color=Theme.BUTTON_INACTIVE_COLOR, font_size=dp(13))
            lbl.bind(size=lambda w, *_a: setattr(w, "text_size", (w.width, w.height)))
            block.add_widget(lbl)
        return block

    def _menu(self, title, items):
        """A small popup of stacked action buttons. `items`: list of (label, callback[, danger])."""
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(8))
        pop = Popup(title=title, title_font=Theme.DEFAULT_FONT,
                    content=box, size_hint=(None, None), size=(dp(260), dp(250)))
        for it in items:
            label, fn = it[0], it[1]
            danger = it[2] if len(it) > 2 else False
            b = SizedRectangleButton(text=label, font_name=Theme.DEFAULT_FONT, size_hint=(1, None), height=dp(46))
            b.background_color = Theme.BOX_BACKGROUND_COLOR
            b.background_radius = dp(8)
            b.outline_color = Theme.ERROR_BORDER_COLOR if danger else Theme.BUTTON_BORDER_COLOR
            b.bind(on_release=lambda _w, _fn=fn: (pop.dismiss(), _fn()))
            box.add_widget(b)
        pop.open()

    def _card_menu(self, e):
        """The per-card '⋯' menu: rename / move / delete (keeps cards uncluttered)."""
        self._menu(e.get("name", "棋形"), [
            ("改名", lambda: self._rename_entry(e)),
            ("移动到…", lambda: self._move_entry(e)),
            ("删除", lambda: self._delete_entry(e), True),
        ])

    def _folder_menu(self, path):
        self._menu(leaf_name(path), [
            ("改名", lambda: self._rename_folder(path)),
            ("移动到…", lambda: self._move_folder(path)),
            ("删除", lambda: self._del_folder_confirm(path), True),
        ])

    def _move_folder(self, path):
        """Move this whole folder (with its sub-folders & boards) under another folder.

        Ask for the DESTINATION PARENT path ('' = root); the folder keeps its own name. e.g.
        moving 'Go-Online' into '网络对弈' makes it '网络对弈/Go-Online'."""
        def on_ok(dest):
            dest = norm_path(dest)                       # target parent; '' = root
            if dest == path or dest.startswith(path + SEP):
                self.katrain and self.katrain.controls.set_status("不能移动到自己或子文件夹里", STATUS_INFO)
                return                                   # would create a cycle
            new_full = (dest + SEP + leaf_name(path)) if dest else leaf_name(path)
            if new_full == path:
                return
            self.lib.rename_category(path, new_full)     # re-prefixes all sub-folders & boards
            if self.current == path or self.current.startswith(path + SEP):
                self.current = new_full + self.current[len(path):]
            self.refresh()
        self._ask_text("移动到哪个文件夹（输入目标文件夹名，留空=根；可用 / 分层，目标不存在会自动新建）",
                       parent_path(path), on_ok)

    # ------------------------------------------------------------ actions --
    def load_entry(self, e):
        if self.katrain:
            self.katrain.library_load(e)
        if self.popup:
            self.popup.dismiss()

    def _new_blank(self):
        if self.katrain:
            self.katrain.library_new_blank(self)
        if self.popup:
            self.popup.dismiss()   # load-and-go: close so the user can place stones

    def _save_current(self):
        if self.katrain:
            self.katrain.library_save_current(popup=self)   # snapshot the live board into the library

    def _import_sgf(self):
        if self.katrain:
            self.katrain.library_import_sgf(popup=self)      # pick an SGF file and add it to the library

    def _capture(self):
        if not self.katrain:
            return
        self.katrain.library_capture(self.current or DEFAULT_CATEGORY, self)

    def _reserved(self, name):
        """Folder names that are empty are not allowed (everything else is fine)."""
        return not (name or "").strip()

    def _new_folder(self):
        """Create a sub-folder inside the current folder (a single leaf name, no '/')."""
        def on_ok(name):
            name = (name or "").strip().replace(SEP, "·")   # a leaf only; '/' would forge a deeper path
            if self._reserved(name):
                return
            full = (self.current + SEP + name) if self.current else name
            self.lib.add_category(full)
            self._enter(full)   # step into the freshly-made folder
        self._ask_text("新建文件夹", "", on_ok)

    def _rename_folder(self, path):
        def on_ok(name):
            name = (name or "").strip().replace(SEP, "·")
            if self._reserved(name) or name == leaf_name(path):
                return
            parent = parent_path(path)
            new_full = (parent + SEP + name) if parent else name
            self.lib.rename_category(path, new_full)
            # keep the user where they were if the rename moved their current folder
            if self.current == path or self.current.startswith(path + SEP):
                self.current = new_full + self.current[len(path):]
            self.refresh()
        self._ask_text("文件夹改名", leaf_name(path), on_ok)

    def _del_folder_confirm(self, path):
        n = len(self.lib.entries(path))
        subs = len(self.lib.child_folders(path))

        def do():
            self.lib.remove_category(path)
            if self.current == path or self.current.startswith(path + SEP):
                self.current = parent_path(path)
            self.refresh()

        self._confirm(f"删除文件夹「{leaf_name(path)}」？",
                      f"将一并删除其 {subs} 个子文件夹；其中 {n} 个棋盘会移到「未分类」（不会删除棋盘）。",
                      do, yes_text="删除", danger=True)

    def _confirm(self, title, message, on_yes, yes_text="确定", danger=False):
        box = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(12))
        msg = self._lbl(message, halign="center", valign="middle")
        msg.bind(size=lambda w, *_a: setattr(w, "text_size", (w.width, w.height)))
        box.add_widget(msg)
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        pop = Popup(title=title, title_font=Theme.DEFAULT_FONT, content=box,
                    size_hint=(None, None), size=(dp(380), dp(210)))

        def yes():
            on_yes()
            pop.dismiss()

        yb = self._btn(yes_text, yes)
        if danger:
            yb.outline_color = Theme.ERROR_BORDER_COLOR
        row.add_widget(yb)
        row.add_widget(self._btn("取消", pop.dismiss))
        box.add_widget(row)
        pop.open()

    def _rename_entry(self, e):
        self._ask_text("改名", e.get("name", ""), lambda name: (self.lib.rename_entry(e["id"], name), self.refresh()))

    def _move_entry(self, e):
        def on_ok(cat):
            cat = norm_path(cat) or DEFAULT_CATEGORY
            self.lib.set_category(e["id"], cat)
            self.refresh()
        # accept a full folder path, e.g. "jinjin/第二节课"; unknown folders are created on the fly
        self._ask_text("移动到文件夹（可用 / 表示层级，自动新建）", e.get("category", DEFAULT_CATEGORY), on_ok)

    def _delete_entry(self, e):
        self.lib.remove_entry(e["id"])
        self.refresh()

    def _ask_text(self, title, initial, on_ok):
        # Use a NATIVE input dialog (proper IME / Chinese input). It runs in a subprocess, so
        # do it off the Kivy thread and apply the result back on the main thread.
        def worker():
            value = _native_text_prompt(title, initial or "")
            if value is not None and value.strip():
                Clock.schedule_once(lambda *_a: on_ok(value.strip()), 0)

        threading.Thread(target=worker, daemon=True).start()
