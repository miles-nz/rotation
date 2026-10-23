// Shared "Use these" / "Save as default" / "Clear default" behaviour for the
// tool pages. Each page keeps its own picker and read-only view; this wires
// the buttons and the note under the read-only view saying whether what's
// shown is the saved default or just for this visit.
//
// Expects on the page: #save-btn ("Use these"), #save-default-btn,
// #picker-error, and in the read-only view #default-note and
// #clear-default-link.

async function postToolDefault(url, value) {
    const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(value),
    });
    let body = null;
    try {
        body = await resp.json();
    } catch (e) {
        // fall through to the generic message
    }
    if (!resp.ok) {
        throw new Error(
            body && body.detail
                ? "Couldn't save the default: " + body.detail
                : "Couldn't save the default. Check your connection and try again.",
        );
    }
    return body.default;
}

// collect() reads the picker and returns the selection, or null after
// showing its own validation error. apply(selection) shows the read-only
// view for it. hasStored says whether the page loaded with a saved default.
function setupDefaultControls({ url, hasStored, collect, apply }) {
    const useBtn = document.getElementById("save-btn");
    const saveBtn = document.getElementById("save-default-btn");
    const errorEl = document.getElementById("picker-error");
    const note = document.getElementById("default-note");
    const clearLink = document.getElementById("clear-default-link");

    function showNote(isDefault) {
        note.textContent = isDefault ? "Saved as your default." : "For this visit only - not saved as your default.";
        clearLink.style.display = isDefault ? "inline" : "none";
    }

    // The page disables "Use these" while its playlists load; keep the
    // two buttons in step.
    saveBtn.disabled = useBtn.disabled;
    new MutationObserver(() => {
        saveBtn.disabled = useBtn.disabled;
    }).observe(useBtn, { attributes: true, attributeFilter: ["disabled"] });

    useBtn.addEventListener("click", () => {
        const selection = collect();
        if (!selection) return;
        apply(selection);
        showNote(false);
    });

    saveBtn.addEventListener("click", async () => {
        const selection = collect();
        if (!selection) return;
        saveBtn.disabled = true;
        try {
            const stored = await postToolDefault(url, selection);
            apply(stored);
            showNote(true);
        } catch (e) {
            errorEl.textContent = e.message;
            errorEl.style.display = "block";
        } finally {
            saveBtn.disabled = useBtn.disabled;
        }
    });

    clearLink.addEventListener("click", async (e) => {
        e.preventDefault();
        try {
            await postToolDefault(url, null);
            note.textContent = "Default cleared - this selection is for this visit only.";
            clearLink.style.display = "none";
        } catch (err) {
            note.textContent = err.message;
        }
    });

    showNote(hasStored);
}
