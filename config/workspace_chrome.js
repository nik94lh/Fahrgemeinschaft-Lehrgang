(() => {
    "use strict";

    const menuSelector = 'details[name="workspace-menu"]';

    function closeOtherMenus(event) {
        for (const menu of document.querySelectorAll(`${menuSelector}[open]`)) {
            if (!menu.contains(event.target)) {
                menu.open = false;
            }
        }
    }

    document.addEventListener("focusin", closeOtherMenus);
    document.addEventListener("pointerdown", closeOtherMenus);
    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape" || event.defaultPrevented || !(event.target instanceof Element)) {
            return;
        }
        const menu = event.target.closest(`${menuSelector}[open]`);
        if (menu) {
            event.preventDefault();
            menu.open = false;
            menu.querySelector("summary").focus();
        }
    });
})();
