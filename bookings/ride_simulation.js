(() => {
    "use strict";
    const form = document.querySelector("#simulation-open-form");
    if (!form || !window.location.hash) {
        return;
    }
    const token = window.location.hash.slice(1);
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    form.elements.namedItem("token").value = token;
    form.requestSubmit();
})();
