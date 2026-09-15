from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urljoin

from .models import ActionType, Locator
from .redaction import redact_text


class SurfaceError(RuntimeError):
    """A live surface could not perform or observe an operation."""


class SurfaceTimeout(SurfaceError):
    """The surface did not reach the requested state before its deadline."""


class UnexpectedDialog(SurfaceError):
    """An unexpected confirmation or browser dialog blocked the surface."""


class SurfaceAppError(SurfaceError):
    """The application returned an outright error while acting."""


@dataclass(frozen=True)
class Control:
    ephemeral_id: str
    kind: str
    name: str
    locator: Locator

    def to_dict(self) -> dict[str, str | dict[str, Any]]:
        return {
            "id": self.ephemeral_id,
            "kind": self.kind,
            "name": self.name,
            "locator": self.locator.to_dict(),
        }


@dataclass(frozen=True)
class ReadableTarget:
    ephemeral_id: str
    text: str
    locator: Locator

    def to_dict(self) -> dict[str, str | dict[str, Any]]:
        return {
            "id": self.ephemeral_id,
            "text": self.text,
            "locator": self.locator.to_dict(),
        }


@dataclass(frozen=True)
class SurfaceObservation:
    url: str
    title: str
    text: str
    controls: tuple[Control, ...]
    readable_targets: tuple[ReadableTarget, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "controls": [control.to_dict() for control in self.controls],
            "readable_targets": [target.to_dict() for target in self.readable_targets],
        }


class BrowserSurface:
    """Playwright-backed surface using semantic locators before CSS fallbacks."""

    kind = "browser"

    def __init__(self, playwright: Any, browser: Any, context: Any, page: Any):
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self.page = page
        self._unexpected_dialog: str | None = None
        self._navigation_guard: Callable[[str], None] | None = None
        self._blocked_navigation_error: Exception | None = None
        self._route_installed = False
        self.page.on("dialog", self._handle_dialog)

    @classmethod
    def open(
        cls,
        url: str,
        *,
        headless: bool = True,
        navigation_guard: Callable[[str], None] | None = None,
    ) -> "BrowserSurface":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise SurfaceError(
                "Playwright is required. Install with: pip install -e '.[browser]' "
                "and run: playwright install chromium"
            ) from exc
        playwright = sync_playwright().start()
        surface: BrowserSurface | None = None
        try:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()
            surface = cls(playwright, browser, context, page)
            if navigation_guard is not None:
                surface.set_navigation_guard(navigation_guard)
            page.goto(url, wait_until="domcontentloaded")
            return surface
        except Exception:
            if surface is not None:
                blocked = surface._take_blocked_navigation()
                surface.close()
                if blocked is not None:
                    raise blocked
            else:
                playwright.stop()
            raise

    @property
    def url(self) -> str:
        return str(self.page.url)

    def close(self) -> None:
        self._context.close()
        self._browser.close()
        self._playwright.stop()

    def set_navigation_guard(self, guard: Callable[[str], None]) -> None:
        self._navigation_guard = guard
        if not self._route_installed:
            self._context.route("**/*", self._route_request)
            self._route_installed = True

    def _route_request(self, route: Any, request: Any) -> None:
        is_document = getattr(request, "resource_type", "") == "document"
        if self._navigation_guard is None or not (request.is_navigation_request() or is_document):
            route.continue_()
            return
        try:
            request_url = str(request.url)
            self._navigation_guard(request_url)
            if is_document:
                response = route.fetch(max_redirects=0)
                location = response.headers.get("location")
                if location:
                    self._navigation_guard(urljoin(request_url, location))
                route.fulfill(response=response)
                return
        except Exception as exc:
            self._blocked_navigation_error = exc
            route.abort(error_code="blockedbyclient")
            return
        route.continue_()

    def observe(self) -> SurfaceObservation:
        try:
            text = str(self.page.locator("body").inner_text())
            self._raise_pending_dialog("observe")
            if "application error" in text.lower():
                raise SurfaceAppError("the page reported an application error")
            title = str(self.page.title())
            controls = tuple(self._controls())
            readable_targets = tuple(self._readable_targets())
            return SurfaceObservation(self.url, title, text, controls, readable_targets)
        except SurfaceError:
            raise
        except Exception as exc:
            raise SurfaceError(f"observe failed: {exc}") from exc

    def _handle_dialog(self, dialog: Any) -> None:
        self._unexpected_dialog = str(getattr(dialog, "message", "browser dialog"))
        try:
            dialog.dismiss()
        except Exception:
            return

    def _raise_pending_dialog(self, action: str) -> None:
        if self._unexpected_dialog is None:
            return
        message = self._unexpected_dialog
        self._unexpected_dialog = None
        raise UnexpectedDialog(f"{action} encountered an unexpected browser dialog: {message}")

    def _take_blocked_navigation(self) -> Exception | None:
        error = self._blocked_navigation_error
        self._blocked_navigation_error = None
        return error

    def _controls(self) -> list[Control]:
        handles = self.page.locator("a,button,input,select,textarea,[role]")
        result: list[Control] = []
        for index in range(handles.count()):
            element = handles.nth(index)
            info = element.evaluate(
                """(el) => {
                  const tag = el.tagName.toLowerCase();
                  const type = (el.getAttribute('type') || '').toLowerCase();
                  const aria = el.getAttribute('aria-label') || '';
                  const label = el.labels && el.labels.length ? el.labels[0].innerText : '';
                  const text = (el.innerText || el.value || aria || label || '').trim();
                  const role = el.getAttribute('role') ||
                    (tag === 'a' ? 'link' : (tag === 'button' || type === 'submit' ? 'button' : 'input'));
                  return {tag, type, aria, label: label.trim(), text, role,
                    name: el.getAttribute('name') || '', id: el.id || '',
                    href: el.getAttribute('href') || ''};
                }"""
            )
            if not info["text"] and not info["aria"] and not info["label"]:
                continue
            locator = self._stable_locator(info)
            name = info["aria"] or info["label"] or info["text"]
            result.append(Control(f"control-{index}", info["role"], name, locator))
        return result

    def _readable_targets(self) -> list[ReadableTarget]:
        handles = self.page.locator("h1,h2,h3,th,td,p,[id]")
        result: list[ReadableTarget] = []
        seen: set[tuple[str, str]] = set()
        for index in range(handles.count()):
            element = handles.nth(index)
            info = element.evaluate(
                """(el) => ({
                  text: (el.innerText || '').trim(),
                  id: el.id || '',
                  tag: el.tagName.toLowerCase()
                })"""
            )
            if not info["text"]:
                continue
            key = (info["id"], info["text"])
            if key in seen:
                continue
            seen.add(key)
            if info["id"]:
                locator = Locator(
                    "css",
                    f"#{info['id']}",
                    rationale="Stable author-provided id for a readable output target.",
                )
            else:
                locator = Locator(
                    "text",
                    info["text"],
                    rationale="Exact visible text fallback for legacy readable content.",
                )
            result.append(ReadableTarget(f"target-{index}", info["text"], locator))
        return result

    @staticmethod
    def _stable_locator(info: dict[str, str]) -> Locator:
        if info["label"]:
            return Locator(
                "label",
                info["label"],
                rationale="Associated form label is more stable than position or generated markup.",
            )
        if info["role"] in {"button", "link"} and info["text"]:
            return Locator(
                "role",
                f"{info['role']}:{info['text']}",
                rationale="Accessible role and visible name survive table/layout changes.",
            )
        if info["name"]:
            return Locator(
                "css",
                f"{info['tag']}[name={_quote_css(info['name'])}]",
                rationale="Named control fallback for legacy markup without test IDs.",
            )
        if info["id"]:
            return Locator(
                "css",
                f"#{info['id']}",
                rationale="Author-provided element id; used only when semantic metadata is absent.",
            )
        if info["href"]:
            return Locator(
                "css",
                f"a[href={_quote_css(info['href'])}]",
                rationale="Exact route is a stable fallback for a legacy link.",
            )
        raise SurfaceError(f"control has no stable locator: {info}")

    def resolve(self, locator: Locator, timeout_ms: int = 5_000) -> Any:
        errors: list[str] = []
        timed_out = False
        for candidate in (locator, *locator.fallback):
            try:
                handle = self._resolve_one(candidate)
                handle.first.wait_for(state="attached", timeout=timeout_ms)
                return handle
            except Exception as exc:
                if "Timeout" in type(exc).__name__:
                    timed_out = True
                errors.append(f"{candidate.strategy}:{candidate.value}: {exc}")
        if timed_out:
            raise SurfaceTimeout(f"could not resolve locator before timeout: {'; '.join(errors)}")
        raise SurfaceError(f"could not resolve locator {locator}: {'; '.join(errors)}")

    def _resolve_one(self, locator: Locator) -> Any:
        try:
            if locator.strategy == "label":
                return self.page.get_by_label(locator.value, exact=True)
            if locator.strategy == "text":
                return self.page.get_by_text(locator.value, exact=True)
            if locator.strategy == "css":
                return self.page.locator(locator.value)
            if locator.strategy == "role":
                role, _, name = locator.value.partition(":")
                return self.page.get_by_role(role, name=name, exact=True)
        except Exception as exc:
            raise SurfaceError(f"could not resolve locator {locator}: {exc}") from exc
        raise SurfaceError(f"unsupported locator strategy: {locator.strategy}")

    def preview_url(self, locator: Locator, timeout_ms: int = 5_000) -> str | None:
        """Return a static link/form destination before a click is performed."""

        try:
            handle = self.resolve(locator, timeout_ms).first
            destination = handle.evaluate(
                """(el) => {
                  const anchor = el.closest('a');
                  if (anchor && anchor.href) return anchor.href;
                  if (el.formAction) return el.formAction;
                  const form = el.closest('form') || el.form;
                  if (form) return new URL(form.getAttribute('action') || location.href, location.href).href;
                  return null;
                }"""
            )
            return str(destination) if destination else None
        except Exception as exc:
            if "Timeout" in type(exc).__name__:
                raise SurfaceTimeout(f"could not inspect click destination before timeout: {exc}") from exc
            raise SurfaceError(f"could not inspect click destination: {exc}") from exc

    def perform(self, action: ActionType, locator: Locator | None = None, value: str | None = None, timeout_ms: int = 5_000) -> None:
        try:
            if action is ActionType.NAVIGATE:
                if value is None:
                    raise SurfaceError("navigate requires a URL")
                self.page.goto(value, wait_until="domcontentloaded", timeout=timeout_ms)
            elif action is ActionType.CLICK:
                if locator is None:
                    raise SurfaceError("click requires a locator")
                self.resolve(locator, timeout_ms).click(timeout=timeout_ms)
                self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            elif action is ActionType.FILL:
                if locator is None or value is None:
                    raise SurfaceError("fill requires a locator and value")
                self.resolve(locator, timeout_ms).fill(value, timeout=timeout_ms)
            elif action is ActionType.PRESS:
                if locator is None or value is None:
                    raise SurfaceError("press requires a locator and key")
                self.resolve(locator, timeout_ms).press(value, timeout=timeout_ms)
            elif action is ActionType.WAIT:
                delay_ms = int(value or "250")
                if delay_ms < 0 or delay_ms > timeout_ms:
                    raise SurfaceTimeout(
                        f"wait duration {delay_ms}ms exceeds declared timeout {timeout_ms}ms"
                    )
                self.page.wait_for_timeout(delay_ms)
            else:
                raise SurfaceError(f"unsupported interactive action: {action.value}")
        except SurfaceError:
            raise
        except Exception as exc:
            blocked = self._take_blocked_navigation()
            if blocked is not None:
                raise blocked
            message = str(exc).lower()
            if "dialog" in message or "confirmation" in message:
                raise UnexpectedDialog(f"{action.value} was blocked by an unexpected dialog: {exc}") from exc
            if "application error" in message:
                raise SurfaceAppError(f"{action.value} hit an application error: {exc}") from exc
            if "Timeout" in type(exc).__name__:
                raise SurfaceTimeout(f"{action.value} timed out: {exc}") from exc
            raise SurfaceError(f"{action.value} failed: {exc}") from exc
        self._raise_blocked_navigation()
        self._raise_pending_dialog(action.value)

    def _raise_blocked_navigation(self) -> None:
        blocked = self._take_blocked_navigation()
        if blocked is not None:
            raise blocked

    def extract(self, locator: Locator, timeout_ms: int = 5_000) -> str:
        try:
            return str(self.resolve(locator, timeout_ms).inner_text(timeout=timeout_ms)).strip()
        except Exception as exc:
            if "Timeout" in type(exc).__name__:
                raise SurfaceTimeout(f"extract timed out: {exc}") from exc
            raise SurfaceError(f"extract failed for {locator}: {exc}") from exc

    def capture(self, directory: Path, stem: str) -> tuple[Path | None, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = directory / f"{stem}.txt"
        text = str(self.page.locator("body").inner_text())
        snapshot.write_text(redact_text(text), encoding="utf-8")
        return None, snapshot


def _quote_css(value: str) -> str:
    return '"' + re.sub(r'(["\\])', r"\\\1", value) + '"'
