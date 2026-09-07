// Adds drag-to-resize handles to a <table>'s header cells, backed by a
// <colgroup> so column widths survive tbody re-renders (e.g. after sorting).
// Widths are persisted to localStorage under `storageKey` when provided.
function makeColumnsResizable(table, { storageKey, minWidth = 40 } = {}) {
    const headerRow = table.querySelector("thead tr");
    if (!headerRow) return;
    const ths = [...headerRow.children];

    let colgroup = table.querySelector("colgroup");
    if (!colgroup) {
        colgroup = document.createElement("colgroup");
        table.insertBefore(colgroup, table.firstChild);
    }
    colgroup.innerHTML = ths.map(() => "<col>").join("");
    const cols = [...colgroup.children];

    function loadSavedWidths() {
        if (!storageKey) return null;
        try {
            const raw = localStorage.getItem(storageKey);
            const parsed = raw ? JSON.parse(raw) : null;
            return Array.isArray(parsed) && parsed.length === cols.length ? parsed : null;
        } catch (e) {
            return null;
        }
    }

    function saveWidths() {
        if (!storageKey) return;
        try {
            localStorage.setItem(storageKey, JSON.stringify(cols.map((c) => c.style.width)));
        } catch (e) {
            // ignore (e.g. private browsing quota)
        }
    }

    const saved = loadSavedWidths();
    cols.forEach((col, i) => {
        col.style.width = (saved && saved[i]) || `${ths[i].getBoundingClientRect().width}px`;
    });
    table.style.tableLayout = "fixed";

    ths.forEach((th, i) => {
        if (th.classList.contains("checkbox-col")) return;

        th.style.position = "relative";
        const handle = document.createElement("span");
        handle.className = "col-resize-handle";
        th.appendChild(handle);

        let startX = 0;
        let startWidth = 0;

        function onMouseMove(e) {
            const newWidth = Math.max(minWidth, startWidth + (e.clientX - startX));
            cols[i].style.width = `${newWidth}px`;
        }
        function onMouseUp() {
            document.removeEventListener("mousemove", onMouseMove);
            document.removeEventListener("mouseup", onMouseUp);
            document.body.style.cursor = "";
            saveWidths();
            // The mouseup that ends a drag fires a click on whatever element is
            // under the cursor (often the header text, not the handle) - swallow
            // it so dragging a column edge never also triggers a column sort.
            document.addEventListener(
                "click",
                (e) => {
                    e.stopPropagation();
                    e.preventDefault();
                },
                { capture: true, once: true },
            );
        }

        handle.addEventListener("mousedown", (e) => {
            e.preventDefault();
            e.stopPropagation();
            startX = e.clientX;
            startWidth = cols[i].getBoundingClientRect().width;
            document.body.style.cursor = "col-resize";
            document.addEventListener("mousemove", onMouseMove);
            document.addEventListener("mouseup", onMouseUp);
        });
    });
}
