"""AI chat Setup page and header model switcher (mixed into AiChat)."""

from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QWidget

from src.tools.ai_assist import (
    BRAVE_KEY_PAGE,
    BRAVE_URL,
    KEY_PAGES,
    PRESETS,
    THINKING_LEVELS,
    AiJob,
    ModelInfo,
    deepseek_balance,
    delete_key,
    fit_thinking,
    is_deepseek,
    list_models,
    load_key,
    save_key,
    thinking_levels,
    valid_key,
    valid_model,
    validate_base_url,
)
from src.ui.chat_widgets import _BUTTON_CSS, _FIELD_CSS, balance_note, friendly_error, model_label
from src.ui.context_menu import style_menu
from src.ui.theme import ACCENT_HEX, DANGER_HEX, TEXT_DIM_HEX, ui_font


class ChatSetupMixin:
    """Methods of AiChat; widgets and state live on AiChat itself."""

    # ------------------------------------------------------------------ setup page

    def _build_setup(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(2, 4, 8, 2)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._provider = QComboBox()
        for key, (label, _url, _model) in PRESETS.items():
            self._provider.addItem(label, key)
        self._provider.addItem("Custom (OpenAI-compatible)", "custom")
        self._provider.currentIndexChanged.connect(self._on_provider_picked)
        self._url = QLineEdit()
        self._url.setPlaceholderText("https://... or http://localhost:port/v1")
        self._model = QLineEdit()
        self._model.setPlaceholderText("model name, e.g. deepseek-flash")
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setToolTip("Stored in Windows Credential Manager, never in state.json")
        self._get_key = QPushButton("Get a key")
        self._get_key.clicked.connect(self._open_key_page)
        self._remove_key = QPushButton("Remove")
        self._remove_key.clicked.connect(lambda: self._forget_key(False))
        key_row = QHBoxLayout()
        key_row.addWidget(self._key, 1)
        key_row.addWidget(self._get_key)
        key_row.addWidget(self._remove_key)
        self._search_key = QLineEdit()
        self._search_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._search_key.setToolTip("Optional Brave Search API key (free tier). A saved DeepSeek key searches through DeepSeek first; with neither, web search uses DuckDuckGo, which often blocks it.")
        get_search = QPushButton("Get a key")
        get_search.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(BRAVE_KEY_PAGE)))
        self._remove_search = QPushButton("Remove")
        self._remove_search.clicked.connect(lambda: self._forget_key(True))
        search_row = QHBoxLayout()
        search_row.addWidget(self._search_key, 1)
        search_row.addWidget(get_search)
        search_row.addWidget(self._remove_search)
        self._thinking = QComboBox()
        self._fill_thinking(self._plugin.base_url, self._plugin.thinking, self._plugin.model)
        for edit in (self._url, self._model):  # the level list follows the model and URL being typed
            edit.textEdited.connect(lambda _t="": self._fill_thinking(
                self._url.text(), self._thinking.currentData() or "off", self._model.text()))
        self._thinking.setToolTip("Think before answering: slower and more tokens, better at math and science")
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._save_btn = QPushButton("Save and test")
        self._save_btn.clicked.connect(self._save_and_test)
        for widget in (self._provider, self._url, self._model, self._key, self._search_key, self._thinking):
            widget.setStyleSheet(_FIELD_CSS)
        for widget in (self._get_key, get_search, self._remove_key, self._remove_search, self._save_btn):
            widget.setStyleSheet(_BUTTON_CSS)
            widget.setCursor(Qt.CursorShape.PointingHandCursor)
        for row_label, field in (("Provider", self._provider), ("Base URL", self._url), ("Model", self._model)):
            form.addRow(self._form_label(row_label), field)
        form.addRow(self._form_label("API key"), key_row)
        form.addRow(self._form_label("Search key"), search_row)
        form.addRow(self._form_label("Thinking"), self._thinking)
        form.addRow("", self._save_btn)
        form.addRow("", self._status)
        return page

    @staticmethod
    def _form_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(ui_font(8.5))
        label.setStyleSheet(f"color: {TEXT_DIM_HEX};")
        return label

    def _refresh_keys(self) -> None:
        """Short placeholders (they must fit beside two buttons) and Remove only when a key is saved."""
        try:
            has_key = bool(load_key(validate_base_url(self._url.text())))
        except ValueError:
            has_key = False
        has_search = bool(load_key(BRAVE_URL))
        self._key.setPlaceholderText("Saved" if has_key else "Paste API key")
        self._search_key.setPlaceholderText("Saved" if has_search else "Optional")
        self._remove_key.setEnabled(has_key)
        self._remove_search.setEnabled(has_search)

    def _forget_key(self, search: bool) -> None:
        try:
            url = BRAVE_URL if search else validate_base_url(self._url.text())
        except ValueError:
            return
        delete_key(url)
        self._refresh_keys()
        self._refresh_title()
        self._set_status("Search key removed." if search else "API key removed.")

    def _fill_thinking(self, url: str, level: str, model: str = "") -> None:
        """Setup's Thinking choices: only the levels this provider has, `level` (or its nearest) selected."""
        self._thinking.clear()
        for each in thinking_levels(url, model):
            self._thinking.addItem(each.title(), each)
        self._thinking.setCurrentIndex(max(0, self._thinking.findData(fit_thinking(url, level, model))))

    def _load_setup(self) -> None:
        p = self._plugin
        self._provider.blockSignals(True)
        self._provider.setCurrentIndex(max(0, self._provider.findData(p.provider)))
        self._provider.blockSignals(False)
        self._url.setText(p.base_url)
        self._url.setEnabled(p.provider == "custom")
        self._model.setText(p.model)
        self._key.clear()
        self._search_key.clear()
        self._refresh_keys()
        self._get_key.setEnabled(p.provider in KEY_PAGES)
        self._fill_thinking(p.base_url, p.thinking, p.model)

    def _on_provider_picked(self) -> None:
        key = self._provider.currentData()
        if key in PRESETS:
            _label, url, model = PRESETS[key]
            self._url.setText(url)
            self._model.setText(model)
        self._url.setEnabled(key == "custom")
        self._get_key.setEnabled(key in KEY_PAGES)
        self._fill_thinking(self._url.text(), self._thinking.currentData() or "off", self._model.text())
        self._refresh_keys()

    def _open_key_page(self) -> None:
        page = KEY_PAGES.get(self._provider.currentData())
        if page:
            QDesktopServices.openUrl(QUrl(page))

    def _set_status(self, text: str, ok: bool | None = None) -> None:
        color = TEXT_DIM_HEX if ok is None else (ACCENT_HEX if ok else DANGER_HEX)
        self._status.setStyleSheet(f"color: {color};")
        self._status.setText(text)
        self._relayout()

    def _save_and_test(self) -> None:
        if self._job is not None:
            return
        provider = self._provider.currentData()
        try:
            url = validate_base_url(self._url.text())
        except ValueError:
            self._set_status("Use an https:// URL (plain http only for localhost).", False)
            return
        model = self._model.text().strip()
        if not valid_model(model):
            self._set_status("Enter a model name (letters, digits, - _ . : / only).", False)
            return
        search_key = self._search_key.text().strip()
        if search_key:
            if not valid_key(search_key) or not save_key(BRAVE_URL, search_key):
                self._set_status("That search key could not be saved.", False)
                return
            self._search_key.clear()
        key = self._key.text().strip()
        if key:
            if not valid_key(key):
                self._set_status("That key does not look right (no spaces, 8 to 512 characters).", False)
                return
            if not save_key(url, key):
                self._set_status("Could not save the key to Windows Credential Manager.", False)
                return
            self._key.clear()
        elif not load_key(url):
            self._set_status("Paste your API key first.", False)
            return
        p = self._plugin
        if provider in PRESETS:
            p.set_provider(provider)
        else:
            p.set_custom_url(url)
        p.model, p.thinking = model, self._thinking.currentData()
        self._refresh_keys()
        self._refresh_title()
        self._set_status("Saved. Testing with a tiny request...")
        self._testing = True
        self._start_job([{"role": "user", "content": "Reply with the single word OK."}])

    # ------------------------------------------------------------------ attachments
    # ------------------------------------------------------------------ model switcher

    def _open_model_menu(self) -> None:
        """Header click: the provider's real model list (GET /models, fetched once per URL) and thinking levels."""
        p = self._plugin
        if p.base_url in self._models or not p.has_key():
            self._show_model_menu(self._models.get(p.base_url, []), "" if p.has_key() else "no key")
            return
        self._fetch_models(show=True)

    def _fetch_models(self, show: bool) -> None:
        """GET /models in the background (free, no tokens); `show` opens the menu when it lands. Without the
        menu it only fills in display names, since replies name just the alias (deepseek-flash, not its version)."""
        p = self._plugin
        if self._models_job is not None or not p.has_key():
            return
        url, key = p.base_url, load_key(p.base_url)
        job = self._models_job = AiJob(self)
        job.finished.connect(lambda models, error, u=url: self._on_models(u, models, error, show))
        job.start(lambda: list_models(url, key))
        if show:
            self._title.setText("Loading models...")
        if self._balance is None:
            self._last_info = None  # an opening fetch is a baseline, not a reply's charge
            self._fetch_balance()

    def _fetch_balance(self) -> None:
        """DeepSeek only: GET /user/balance (free) in the background. The drop since the last fetch is added to
        the newest reply's details line (approximate: a Watch pre-fetch in between counts too)."""
        p = self._plugin
        if self._balance_job is not None or not is_deepseek(p.base_url) or not p.has_key():
            return
        key, info = load_key(p.base_url), self._last_info
        job = self._balance_job = AiJob(self)
        job.finished.connect(lambda got, error: self._on_balance(got, error, info))
        job.start(lambda: deepseek_balance(key))

    def _on_balance(self, got: tuple[str, float] | None, error: str, info: QLabel | None) -> None:
        if self._balance_job is not None:
            self._balance_job.deleteLater()
        self._balance_job = None
        if error or got is None:
            return
        self._balance = got
        if self._balance_start is None or self._balance_start[0] != got[0]:
            self._balance_start = got
        if info is not None and info is self._last_info:
            try:
                info.setText(f"{info.text()}  -  {balance_note(self._balance_start, got)}")
            except RuntimeError:  # the row was cleared (New chat) while the fetch ran
                pass
        self._refresh_title()

    def _model_name(self, model_id: str) -> str:
        """The provider's display name for a model id ("" until the list has loaded or when it gives none)."""
        return next((m.name for m in self._models.get(self._plugin.base_url, []) if m.id == model_id), "")

    def _on_models(self, url: str, models: list[ModelInfo] | None, error: str, show: bool = True) -> None:
        if self._models_job is not None:
            self._models_job.deleteLater()
        self._models_job = None
        if models:
            self._models[url] = models
        self._refresh_title()
        if show and url == self._plugin.base_url and self.isVisible():
            self._show_model_menu(models or [], error or ("" if models else "empty list"))

    def _show_model_menu(self, models: list[ModelInfo], error: str = "") -> None:
        p = self._plugin
        menu = style_menu(QMenu(self))
        menu.addSection("Model")
        if p.model and all(m.id != p.model for m in models):
            models = [ModelInfo(p.model), *models]
        for model in models:
            action = menu.addAction(model_label(model))
            action.setCheckable(True)
            action.setChecked(model.id == p.model)
            action.triggered.connect(lambda _c=False, m=model.id: self._pick(model=m))
        if error:
            menu.addAction(f"Could not list models ({friendly_error(error)})").setEnabled(False)
        menu.addSection("Thinking")
        for level in thinking_levels(p.base_url, p.model):
            action = menu.addAction(level.title())
            action.setCheckable(True)
            action.setChecked(level == fit_thinking(p.base_url, p.thinking, p.model))
            action.triggered.connect(lambda _c=False, t=level: self._pick(thinking=t))
        menu.addSeparator()
        menu.addAction("Setup...").triggered.connect(lambda _c=False: self.show_setup())
        menu.exec(self._title.mapToGlobal(QPoint(0, self._title.height() + 2)))
        menu.deleteLater()

    def _pick(self, model: str | None = None, thinking: str | None = None) -> None:
        p = self._plugin
        if model is not None and valid_model(model):
            p.model = model
        if thinking in THINKING_LEVELS:
            p.thinking = thinking
        self._refresh_title()

    def _leave_setup_if_ok(self) -> None:
        if self._stack.currentIndex() == 1 and self._ready():
            self._stack.setCurrentIndex(0)
            self._panel.setVisible(bool(self._history))
            self._relayout()
            self._draft.setFocus()
