from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from .models import ActionType, Locator
from .policy import PolicyViolation


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
        self._websocket_route_installed = False
        self._sensitive_values: set[str] = set()
        self._page_error: str | None = None
        self.page.on("dialog", self._handle_dialog)
        self.page.on("pageerror", self._handle_page_error)

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
            context = browser.new_context(service_workers="block")
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
        if not self._websocket_route_installed:
            route_web_socket = getattr(self._context, "route_web_socket", None)
            if not callable(route_web_socket):
                route_web_socket = getattr(self.page, "route_web_socket", None)
            if callable(route_web_socket):
                route_web_socket("**/*", self._route_websocket)
                self._websocket_route_installed = True
        policy = getattr(guard, "__self__", None)
        if policy is not None and hasattr(policy, "allowed_origins"):
            origins = tuple(policy.allowed_origins)
            route_prefixes = tuple(policy.allowed_route_prefixes)
            script = _websocket_guard_script(origins, route_prefixes)
            worker_script = _worker_guard_script()
            self._context.add_init_script(script)
            self._context.add_init_script(worker_script)
            self.page.evaluate(script)
            self.page.evaluate(worker_script)

    def set_sensitive_values(self, values: set[str]) -> None:
        self._sensitive_values.update(str(value) for value in values if value)

    def _route_request(self, route: Any, request: Any) -> None:
        if self._navigation_guard is None:
            route.continue_()
            return
        try:
            request_url = str(request.url)
            self._navigation_guard(request_url)
            response = route.fetch(max_redirects=0)
            location = response.headers.get("location")
            if location:
                self._navigation_guard(urljoin(request_url, location))
                raise PolicyViolation("redirect responses are not permitted by the route allowlist")
            route.fulfill(response=response)
            return
        except Exception as exc:
            self._blocked_navigation_error = exc
            route.abort(error_code="blockedbyclient")
            return
        route.continue_()

    def _route_websocket(self, websocket: Any) -> None:
        """Enforce the same destination policy for page and worker sockets."""

        if self._navigation_guard is None:
            websocket.connect_to_server()
            return
        try:
            self._navigation_guard(_websocket_policy_url(str(websocket.url)))
        except Exception as exc:
            self._blocked_navigation_error = exc
            websocket.close(code=1008, reason="WebSocket URL is not allowlisted")
            return
        websocket.connect_to_server()

    def observe(self) -> SurfaceObservation:
        try:
            text = str(self.page.locator("body").inner_text())
            self._raise_pending_dialog("observe")
            if "application error" in text.lower():
                raise SurfaceAppError("the page reported an application error")
            title = str(self.page.title())
            controls = tuple(self._controls())
            readable_targets = tuple(self._readable_targets())
            self._raise_blocked_navigation()
            self._raise_pending_page_error("observe")
            self._raise_pending_dialog("observe")
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

    def _handle_page_error(self, error: Any) -> None:
        self._page_error = str(error)

    def _raise_pending_page_error(self, action: str) -> None:
        if self._page_error is None:
            return
        message = self._page_error
        self._page_error = None
        if "websocket url is not allowlisted" in message.lower():
            raise PolicyViolation("WebSocket URL is not allowlisted")
        if "disabled by policy" in message.lower() or "not allowlisted" in message.lower():
            raise PolicyViolation(f"browser context was blocked by policy: {message}")
        raise SurfaceAppError(f"{action} produced a page error: {message}")

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
            if not info["text"] and not info["aria"] and not info["label"] and not info["name"] and not info["id"]:
                continue
            locator = self._stable_locator(info)
            name = info["aria"] or info["label"] or info["text"] or info["name"] or info["id"]
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
                    f"[id={_quote_css(info['id'])}]",
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
        if info["id"]:
            return Locator(
                "css",
                f"[id={_quote_css(info['id'])}]",
                rationale="Author-provided element id is the most stable available control locator.",
            )
        if info["label"]:
            return Locator(
                "label",
                info["label"],
                rationale="Associated form label is more stable than position or generated markup.",
            )
        if info["aria"]:
            return Locator(
                "css",
                f'{info["tag"]}[aria-label={_quote_css(info["aria"])}]',
                rationale="ARIA label fallback for controls without an associated label.",
            )
        if info["name"]:
            return Locator(
                "css",
                f"{info['tag']}[name={_quote_css(info['name'])}]",
                rationale="Named control fallback for legacy markup without test IDs.",
            )
        if info["href"]:
            return Locator(
                "css",
                f"a[href={_quote_css(info['href'])}]",
                rationale="Exact route is a stable fallback for a legacy link.",
            )
        if info["role"] in {"button", "link"} and info["text"]:
            return Locator(
                "role",
                f"{info['role']}:{info['text']}",
                rationale="Accessible role and visible name fallback when no stable DOM attribute exists.",
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
                self.resolve(locator, timeout_ms).click(timeout=timeout_ms, no_wait_after=True)
                self._wait_for_runtime_guard(timeout_ms, action.value)
                self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            elif action is ActionType.FILL:
                if locator is None or value is None:
                    raise SurfaceError("fill requires a locator and value")
                self.resolve(locator, timeout_ms).fill(value, timeout=timeout_ms)
            elif action is ActionType.PRESS:
                if locator is None or value is None:
                    raise SurfaceError("press requires a locator and key")
                self.resolve(locator, timeout_ms).press(value, timeout=timeout_ms, no_wait_after=True)
                self._wait_for_runtime_guard(timeout_ms, action.value)
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
            if "websocket url is not allowlisted" in message:
                raise PolicyViolation("WebSocket URL is not allowlisted") from exc
            if "not allowlisted" in message:
                raise PolicyViolation(f"{action.value} was blocked by the allowlist: {exc}") from exc
            if "disabled by policy" in message:
                raise PolicyViolation(f"{action.value} was blocked by policy: {exc}") from exc
            if "dialog" in message or "confirmation" in message:
                raise UnexpectedDialog(f"{action.value} was blocked by an unexpected dialog: {exc}") from exc
            if "application error" in message:
                raise SurfaceAppError(f"{action.value} hit an application error: {exc}") from exc
            if "Timeout" in type(exc).__name__:
                raise SurfaceTimeout(f"{action.value} timed out: {exc}") from exc
            raise SurfaceError(f"{action.value} failed: {exc}") from exc
        self._raise_blocked_navigation()
        self._raise_pending_page_error(action.value)
        self._raise_pending_dialog(action.value)

    def _raise_blocked_navigation(self) -> None:
        blocked = self._take_blocked_navigation()
        if blocked is not None:
            raise blocked

    def _wait_for_runtime_guard(self, timeout_ms: int, action: str) -> None:
        """Give asynchronous route/page-error callbacks a bounded chance to run."""

        deadline = time.monotonic() + min(timeout_ms, 250) / 1000
        while self._blocked_navigation_error is None and self._page_error is None:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            self.page.wait_for_timeout(min(10, remaining_ms))
        self._raise_blocked_navigation()
        self._raise_pending_page_error(action)

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
        # A generic redaction pass cannot reliably identify every name or
        # regulated value, especially in Unicode text. Persist no page text.
        snapshot.write_text("<REDACTED page content>\n", encoding="utf-8")
        return None, snapshot


def _websocket_guard_script(origins: tuple[str, ...], route_prefixes: tuple[str, ...]) -> str:
    return """
(() => {
  const allowedOrigins = %s;
  const allowedPrefixes = %s;
  const original = window.WebSocket;
  const isAllowed = (raw) => {
    const parsed = new URL(raw, window.location.href);
    const origin = parsed.protocol === 'wss:' ? `https://${parsed.host}` : `http://${parsed.host}`;
    if (!allowedOrigins.includes(origin)) return false;
    if (!allowedPrefixes.length) return true;
    return allowedPrefixes.some((prefix) =>
      prefix === '/' ? parsed.pathname === '/' :
      (parsed.pathname === prefix || parsed.pathname.startsWith(prefix.replace(/\\/$/, '') + '/'))
    );
  };
  function GuardedWebSocket(url, protocols) {
    if (!isAllowed(url)) throw new Error('WebSocket URL is not allowlisted');
    return protocols === undefined ? new original(url) : new original(url, protocols);
  }
  GuardedWebSocket.prototype = original.prototype;
  GuardedWebSocket.CONNECTING = original.CONNECTING;
  GuardedWebSocket.OPEN = original.OPEN;
  GuardedWebSocket.CLOSING = original.CLOSING;
  GuardedWebSocket.CLOSED = original.CLOSED;
  window.WebSocket = GuardedWebSocket;
})();
""" % (json.dumps(origins), json.dumps(route_prefixes))


def _worker_guard_script() -> str:
    """Disable worker contexts that cannot share the page network guard."""

    return """
(() => {
  const disableWorker = (name) => {
    const OriginalWorker = window[name];
    if (!OriginalWorker || OriginalWorker.__cuaGuarded) return;
    function GuardedWorker(url, options) {
      throw new Error(`${name} is disabled by policy`);
    }
    GuardedWorker.__cuaGuarded = true;
    window[name] = GuardedWorker;
  };
  disableWorker('Worker');
  disableWorker('SharedWorker');
})();
"""


def _websocket_policy_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"ws", "wss"}:
        return value
    return parsed._replace(
        scheme="https" if parsed.scheme == "wss" else "http",
        fragment="",
    ).geturl()


def _quote_css(value: str) -> str:
    return '"' + re.sub(r'(["\\])', r"\\\1", value) + '"'
