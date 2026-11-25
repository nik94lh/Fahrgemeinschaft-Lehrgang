(() => {
    "use strict";

    const selector = "#calendar-month-timeline[data-calendar-anchor][data-calendar-center][data-live-preserve]";
    const daySelector = ".calendar-month-track time[data-calendar-date]";
    const timeline = document.querySelector(selector);
    if (!timeline) {
        return;
    }

    let activeRequest;
    let failedShift;
    let retryTimer;
    let frame;

    function days(viewport) {
        return Array.from(viewport.querySelectorAll(daySelector));
    }

    function leftEdge(viewport) {
        return viewport.getBoundingClientRect().left + viewport.clientLeft;
    }

    function dayAtLeft(viewport) {
        const left = leftEdge(viewport);
        for (const day of days(viewport)) {
            const bounds = day.getBoundingClientRect();
            if (bounds.right > left) {
                return {date: day.dataset.calendarDate, offset: left - bounds.left};
            }
        }
        return null;
    }

    function alignDay(viewport, day, offset = 0) {
        const left = viewport.scrollLeft + day.getBoundingClientRect().left - leftEdge(viewport) + offset;
        viewport.scrollTo({left, behavior: "instant"});
    }

    function courseHasFocus(viewport) {
        return document.activeElement !== viewport && viewport.contains(document.activeElement);
    }

    function nearbyEdge(viewport) {
        const {scrollLeft, scrollWidth, clientWidth} = viewport;
        if (scrollWidth <= clientWidth) {
            return null;
        }
        if (scrollLeft < clientWidth && viewport.hasAttribute("data-calendar-before")) {
            return "before";
        }
        if (scrollWidth - clientWidth - scrollLeft < clientWidth && viewport.hasAttribute("data-calendar-after")) {
            return "after";
        }
        return null;
    }

    async function shiftWindow(viewport, range, edge) {
        const page = window.location.href;
        const selected = viewport.dataset.calendarSelected;
        if (!selected) {
            return;
        }
        const url = new URL(page);
        url.searchParams.delete("month_number");
        url.searchParams.delete("year");
        url.searchParams.set("month", selected);
        url.searchParams.set("range", range);

        const request = new AbortController();
        activeRequest = request;
        const stillOnPage = () => viewport.isConnected && window.location.href === page;
        const timeout = window.setTimeout(() => request.abort("timeout"), 10_000);
        try {
            const response = await fetch(url.pathname + url.search, {
                headers: {"X-BMS-Live": "1"},
                mode: "same-origin",
                credentials: "same-origin",
                cache: "no-store",
                redirect: "error",
                signal: request.signal,
            });
            const type = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
            if (!response.ok || type !== "text/html") {
                throw new Error("Invalid calendar window response");
            }
            const fragment = document.createElement("template");
            fragment.innerHTML = await response.text();
            if (request.signal.aborted || !stillOnPage() || courseHasFocus(viewport) || nearbyEdge(viewport) !== edge) {
                return;
            }
            const timelines = fragment.content.querySelectorAll(selector);
            const incoming = timelines[0];
            if (timelines.length !== 1 || fragment.content.querySelector("script")
                || incoming.dataset.calendarCenter !== range || incoming.dataset.calendarSelected !== selected) {
                throw new Error("Invalid calendar window");
            }

            const visible = dayAtLeft(viewport);
            const day = days(incoming).find((tick) => tick.dataset.calendarDate === visible?.date);
            if (!day) {
                failedShift = {viewport, range};
                return;
            }
            // Keep the scroll container and its focus while replacing the dates and courses.
            viewport.replaceChildren(...incoming.childNodes);
            viewport.dataset.calendarCenter = range;
            viewport.dataset.calendarAnchor = incoming.dataset.calendarAnchor;
            for (const edge of ["data-calendar-before", "data-calendar-after"]) {
                viewport.toggleAttribute(edge, incoming.hasAttribute(edge));
            }
            alignDay(viewport, day, visible.offset);
            window.history.replaceState(window.history.state, "", url);
            clearFailedShift();
        } catch {
            if (stillOnPage() && (!request.signal.aborted || request.signal.reason === "timeout")) {
                clearFailedShift();
                failedShift = {viewport, range};
                retryTimer = window.setTimeout(() => {
                    failedShift = undefined;
                    scheduleCheck();
                }, 15_000);
            }
        } finally {
            window.clearTimeout(timeout);
            activeRequest = undefined;
            if (!request.signal.aborted) {
                // The user or a live update may have moved to a different edge during the request.
                scheduleCheck();
            }
        }
    }

    function checkEdges() {
        const viewport = document.querySelector(selector);
        if (!viewport || activeRequest || document.visibilityState === "hidden" || courseHasFocus(viewport)) {
            return;
        }
        const edge = nearbyEdge(viewport);
        if (!edge) {
            clearFailedShift();
            return;
        }
        const range = dayAtLeft(viewport)?.date.slice(0, 7);
        if (range && range !== viewport.dataset.calendarCenter
            && (failedShift?.viewport !== viewport || failedShift.range !== range)) {
            shiftWindow(viewport, range, edge);
        }
    }

    function clearFailedShift() {
        failedShift = undefined;
        window.clearTimeout(retryTimer);
    }

    function scheduleCheck() {
        if (frame === undefined) {
            frame = window.requestAnimationFrame(() => {
                frame = undefined;
                checkEdges();
            });
        }
    }

    const ticks = days(timeline);
    const initial = ticks.find((day) => day.dataset.calendarDate === timeline.dataset.calendarAnchor)
        ?? ticks.find((day) => day.dataset.calendarDate === `${timeline.dataset.calendarCenter}-01`);
    if (initial) {
        alignDay(timeline, initial);
    }

    // The live refresh may replace the whole timeline. Scroll does not bubble.
    document.addEventListener("scroll", (event) => {
        if (event.target instanceof Element && event.target.matches(selector)) {
            scheduleCheck();
        }
    }, {capture: true, passive: true});
    document.addEventListener("focusout", (event) => {
        if (event.target instanceof Element && event.target.closest(selector)) {
            scheduleCheck();
        }
    });
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
            scheduleCheck();
        }
    });
    window.addEventListener("pagehide", () => {
        activeRequest?.abort();
        window.cancelAnimationFrame(frame);
        frame = undefined;
        clearFailedShift();
    });
    window.addEventListener("pageshow", (event) => {
        if (event.persisted) {
            scheduleCheck();
        }
    });
})();
