(() => {
    "use strict";

    function openCode() {
        const code = window.location.hash.slice(1);
        if (!code) {
            return;
        }
        window.history.replaceState(null, "", window.location.pathname);
        const form = document.getElementById("ride-portal-code");
        const legacyCode = /^[A-Za-z0-9_-]{43}$/;
        const shortCode = /^(?:[0-9A-HJKMNP-TV-Z]{4}-){3}[0-9A-HJKMNP-TV-Z]{4}$/i;
        if (form && (shortCode.test(code) || legacyCode.test(code))) {
            form.elements.namedItem("code").value = code;
            form.requestSubmit();
        }
    }

    window.addEventListener("hashchange", openCode);
    openCode();
})();
