// Shared by every Listening Visuals view: remembers the chosen
// visualisation and each one's range in localStorage, and turns a range
// picked in local days into the UTC timestamps the server's prepare routes
// take (the server runs in UTC, so local days are only known here).
const ListeningVisuals = (() => {
    const STORAGE_KEY = "listeningVisuals";
    const DEFAULT_SETTINGS = { range: "30" };
    let visuals = [];

    function load() {
        try {
            return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
        } catch (e) {
            return {};
        }
    }

    function save(saved) {
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
        } catch (e) {
            // Private browsing or storage disabled - just don't remember.
        }
    }

    function remember(viewKey, settings) {
        const saved = load();
        saved.view = viewKey;
        saved[viewKey] = settings;
        save(saved);
    }

    function startOfDay(date) {
        return new Date(date.getFullYear(), date.getMonth(), date.getDate());
    }

    function addDays(date, days) {
        return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
    }

    // "YYYY-MM-DD" (as from <input type="date">) -> local midnight, or null.
    function parseDate(value) {
        const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
        return match ? new Date(+match[1], +match[2] - 1, +match[3]) : null;
    }

    function formatDate(date) {
        const pad = (n) => String(n).padStart(2, "0");
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
    }

    // {range: "7" | "30" | "90" | "custom", start, end} -> the local days it
    // covers (presets end today; custom dates are inclusive) as
    // {fromTs, toTs} in unix seconds, toTs exclusive.
    function rangeBounds(settings) {
        let start = null;
        let end = null;
        if (settings.range === "custom") {
            start = parseDate(settings.start);
            end = parseDate(settings.end);
        }
        if (!start || !end) {
            const days = parseInt(settings.range, 10) || 30;
            end = startOfDay(new Date());
            start = addDays(end, -(days - 1));
        }
        return {
            fromTs: Math.floor(start.getTime() / 1000),
            toTs: Math.floor(addDays(end, 1).getTime() / 1000),
            start: formatDate(start),
            end: formatDate(end),
        };
    }

    function prepareUrl(viewKey, settings) {
        const visual = visuals.find((v) => v.key === viewKey) || visuals[0];
        const bounds = rangeBounds(settings);
        const params = new URLSearchParams({
            from_ts: bounds.fromTs,
            to_ts: bounds.toTs,
            range: settings.range,
            start: bounds.start,
            end: bounds.end,
        });
        return `${visual.prepare_url}?${params}`;
    }

    function open(viewKey, settings) {
        window.location.assign(prepareUrl(viewKey, settings));
    }

    function savedSettings(viewKey) {
        return load()[viewKey] || DEFAULT_SETTINGS;
    }

    // The bare /listening-visuals page: go straight to the last-used
    // visualisation and range. replace(), so Back doesn't land here and
    // bounce forward again.
    function openSaved() {
        const saved = load();
        const viewKey = visuals.some((v) => v.key === saved.view) ? saved.view : visuals[0].key;
        window.location.replace(prepareUrl(viewKey, savedSettings(viewKey)));
    }

    function bindTabs() {
        document.querySelectorAll(".visual-tabs a[data-view]").forEach((tab) => {
            tab.addEventListener("click", (event) => {
                event.preventDefault();
                open(tab.dataset.view, savedSettings(tab.dataset.view));
            });
        });
    }

    function init(visualList) {
        visuals = visualList;
    }

    return { init, open, openSaved, bindTabs, remember, savedSettings, rangeBounds, parseDate, addDays, startOfDay, formatDate };
})();
