(() => {
    "use strict";

    function openCode() {
        const code = window.location.hash.slice(1);
        if (!code) {
            return;
        }
        window.history.replaceState(null, "", window.location.pathname);
        const form = document.getElementById("ride-portal-code");
        if (form && /^[A-Za-z0-9_-]{43}$/.test(code)) {
            form.elements.namedItem("code").value = code;
            form.requestSubmit();
        }
    }

    window.addEventListener("hashchange", openCode);
    openCode();
})();
