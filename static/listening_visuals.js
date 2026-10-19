// Shared by every Listening Visuals view: remembers the chosen
// visualisation and each one's range in localStorage, and turns a range
// picked in local days into the UTC timestamps the server's prepare routes
// take (the server runs in UTC, so local days are only known here).
const ListeningVisuals = (() => {
    const STORAGE_KEY = "listeningVisuals";
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

    // {range, start, end, month} -> the local days it covers as
    // {fromTs, toTs} in unix seconds (toTs exclusive), plus the first and
    // last day. range is a number of days ("30"), months ("12m") or "all" -
    // each ending today - "custom" for the inclusive start and end dates,
    // or "month" for the calendar month in `month` ("YYYY-MM", default this
    // month). "all" starts at 0 and has no start date; the server only
    // returns what exists.
    function rangeBounds(settings) {
        const today = startOfDay(new Date());
        let start = null;
        let end = null;
        if (settings.range === "month") {
            const match = /^(\d{4})-(\d{2})$/.exec(settings.month || "");
            start = match ? new Date(+match[1], +match[2] - 1, 1) : new Date(today.getFullYear(), today.getMonth(), 1);
            end = new Date(start.getFullYear(), start.getMonth() + 1, 0);
        }
        if (settings.range === "custom") {
            start = parseDate(settings.start);
            end = parseDate(settings.end);
        }
        if (settings.range === "all") {
            return { fromTs: 0, toTs: Math.floor(addDays(today, 1).getTime() / 1000), start: "", end: formatDate(today) };
        }
        if (!start || !end) {
            end = today;
            const months = /^(\d+)m$/.exec(settings.range || "");
            if (months) {
                start = new Date(end.getFullYear(), end.getMonth() - Number(months[1]), end.getDate() + 1);
            } else {
                start = addDays(end, -((parseInt(settings.range, 10) || 30) - 1));
            }
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
            month: bounds.start.slice(0, 7),
        });
        return `${visual.prepare_url}?${params}`;
    }

    function open(viewKey, settings) {
        window.location.assign(prepareUrl(viewKey, settings));
    }

    function savedSettings(viewKey) {
        const visual = visuals.find((v) => v.key === viewKey);
        return load()[viewKey] || { range: visual ? visual.default_range : "30" };
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

    // Colour conversions shared by the visualisations' album colours.
    function hexToRgb(hex) {
        const n = parseInt(hex.slice(1), 16);
        return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    }

    function rgbToHsl([r, g, b]) {
        [r, g, b] = [r / 255, g / 255, b / 255];
        const max = Math.max(r, g, b);
        const min = Math.min(r, g, b);
        const l = (max + min) / 2;
        if (max === min) return [0, 0, l];
        const d = max - min;
        const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
        let h;
        if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
        else if (max === g) h = (b - r) / d + 2;
        else h = (r - g) / d + 4;
        return [h / 6, s, l];
    }

    function hslToRgb([h, s, l]) {
        if (s === 0) return [l, l, l].map((c) => Math.round(c * 255));
        const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
        const p = 2 * l - q;
        const channel = (t) => {
            if (t < 0) t += 1;
            if (t > 1) t -= 1;
            if (t < 1 / 6) return p + (q - p) * 6 * t;
            if (t < 1 / 2) return q;
            if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
            return p;
        };
        return [channel(h + 1 / 3), channel(h), channel(h - 1 / 3)].map((c) => Math.round(c * 255));
    }

    function init(visualList) {
        visuals = visualList;
    }

    return {
        init,
        open,
        openSaved,
        bindTabs,
        remember,
        savedSettings,
        rangeBounds,
        parseDate,
        addDays,
        startOfDay,
        formatDate,
        hexToRgb,
        rgbToHsl,
        hslToRgb,
    };
})();
