// Timestamps are stored in UTC; show them in the viewer's own timezone.
for (const el of document.querySelectorAll("time.local-time")) {
    const date = new Date(el.getAttribute("datetime"));
    if (!isNaN(date)) {
        el.textContent = date.toLocaleString(undefined, {
            dateStyle: "medium",
            timeStyle: "short",
        });
    }
}
