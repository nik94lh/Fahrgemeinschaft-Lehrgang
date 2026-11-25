(() => {
    "use strict";

    const regionSelector = "[data-live-region]";
    const stateSelector = "[id][data-live-preserve]";
    const controlSelector = "input:not([type=hidden]), select, textarea, [contenteditable]";
    const interval = 15_000;
    const status = document.getElementById("live-update-status");
    const dirtyControls = new WeakSet();
    let timer;
    let activeRequest;
    let suspended = false;
    let refreshImmediately = false;

    if (!document.querySelector(regionSelector)) {
        return;
    }

    function showStatus(message = "") {
        if (status && status.textContent !== message) {
            status.hidden = !message;
            status.textContent = message;
        }
    }

    function markDirty(event) {
        if (event.target instanceof Element) {
            const control = event.target.closest(controlSelector);
            if (control) {
                dirtyControls.add(control);
            }
        }
    }

    function hasActiveEditing(region) {
        if (region.contains(document.activeElement)) {
            return true;
        }
        const selection = document.getSelection();
        if (selection && !selection.isCollapsed) {
            for (let index = 0; index < selection.rangeCount; index += 1) {
                if (selection.getRangeAt(index).intersectsNode(region)) {
                    return true;
                }
            }
        }
        return Array.from(region.querySelectorAll(controlSelector)).some(
            (control) => dirtyControls.has(control) || (control.type === "file" && control.files?.length),
        );
    }

    function comparableContent(region) {
        const copy = region.cloneNode(true);
        for (const token of copy.querySelectorAll('input[name="csrfmiddlewaretoken"]')) {
            token.removeAttribute("value");
        }
        for (const details of copy.querySelectorAll(`details${stateSelector}`)) {
            details.removeAttribute("open");
        }
        return copy.innerHTML;
    }

    function preservedState(region) {
        const state = new Map();
        for (const element of region.querySelectorAll(stateSelector)) {
            state.set(element.id, {
                left: element.scrollLeft,
                top: element.scrollTop,
                open: element instanceof HTMLDetailsElement ? element.open : undefined,
            });
        }
        return state;
    }

    function replaceRegion(current, incoming) {
        const state = preservedState(current);
        const {scrollLeft, scrollTop} = current;
        current.replaceChildren(...incoming.childNodes);
        for (const element of current.querySelectorAll(stateSelector)) {
            const saved = state.get(element.id);
            if (saved) {
                if (saved.open !== undefined && element instanceof HTMLDetailsElement) {
                    element.open = saved.open;
                }
                element.scrollLeft = saved.left;
                element.scrollTop = saved.top;
            }
        }
        current.scrollLeft = scrollLeft;
        current.scrollTop = scrollTop;
    }

    function applyResponse(html) {
        const template = document.createElement("template");
        template.innerHTML = html;
        const regions = new Map();
        for (const region of template.content.querySelectorAll(regionSelector)) {
            const name = region.dataset.liveRegion;
            if (!name || regions.has(name)) {
                throw new Error("Invalid live update regions");
            }
            regions.set(name, region);
        }

        const targets = Array.from(document.querySelectorAll(regionSelector)).filter(
            (region) => regions.has(region.dataset.liveRegion),
        );
        if (!targets.length || template.content.querySelector("script")) {
            throw new Error("Invalid live update fragment");
        }

        let pending = false;
        for (const target of targets) {
            const incoming = regions.get(target.dataset.liveRegion);
            if (comparableContent(target) === comparableContent(incoming)) {
                continue;
            }
            if (hasActiveEditing(target)) {
                pending = true;
                continue;
            }
            replaceRegion(target, incoming);
        }
        showStatus(pending ? "Neue Daten liegen vor. Die Ansicht wird nach deiner Bearbeitung aktualisiert." : "");
    }

    function canRefresh() {
        return !suspended && document.visibilityState === "visible";
    }

    function schedule(immediate = false) {
        refreshImmediately ||= immediate;
        window.clearTimeout(timer);
        if (canRefresh() && !activeRequest) {
            timer = window.setTimeout(refresh, refreshImmediately ? 0 : interval);
        }
    }

    function cancel() {
        window.clearTimeout(timer);
        activeRequest?.abort();
    }

    async function refresh() {
        if (!canRefresh() || activeRequest) {
            return;
        }
        refreshImmediately = false;
        const path = window.location.pathname + window.location.search;
        const page = window.location.href;
        const request = new AbortController();
        activeRequest = request;
        const isCurrent = () => !request.signal.aborted && canRefresh() && window.location.href === page;
        const timeout = window.setTimeout(() => request.abort("timeout"), 10_000);

        try {
            const response = await fetch(path, {
                method: "GET",
                headers: {"X-BMS-Live": "1"},
                mode: "same-origin",
                credentials: "same-origin",
                cache: "no-store",
                redirect: "error",
                signal: request.signal,
            });
            if (!isCurrent()) {
                return;
            }
            if (response.status === 401 || response.status === 403) {
                suspended = true;
                showStatus("Die automatische Aktualisierung wurde unterbrochen. Öffne die Seite erneut.");
                return;
            }
            const type = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
            if (!response.ok || response.redirected || type !== "text/html") {
                throw new Error("Invalid live update response");
            }
            const html = await response.text();
            if (isCurrent()) {
                applyResponse(html);
            }
        } catch {
            if (isCurrent() || (request.signal.reason === "timeout" && canRefresh() && window.location.href === page)) {
                showStatus("Die automatische Aktualisierung ist gerade nicht möglich. BMS versucht es erneut.");
            }
        } finally {
            window.clearTimeout(timeout);
            activeRequest = undefined;
            schedule();
        }
    }

    document.addEventListener("input", markDirty);
    document.addEventListener("change", markDirty);
    document.addEventListener("reset", (event) => {
        const form = event.target;
        queueMicrotask(() => {
            if (!event.defaultPrevented && form instanceof HTMLFormElement) {
                for (const control of form.elements) {
                    dirtyControls.delete(control);
                }
            }
        });
    });
    document.addEventListener("submit", (event) => {
        suspended = true;
        cancel();
        queueMicrotask(() => {
            if (event.defaultPrevented) {
                suspended = false;
                schedule(true);
            }
        });
    }, true);
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
            schedule(true);
        } else {
            cancel();
        }
    });
    window.addEventListener("pagehide", () => {
        suspended = true;
        cancel();
    });
    window.addEventListener("pageshow", (event) => {
        if (event.persisted) {
            suspended = false;
            schedule(true);
        }
    });
    for (const event of ["popstate", "hashchange"]) {
        window.addEventListener(event, () => {
            cancel();
            schedule(true);
        });
    }

    schedule();
})();
