// Shared field/operator/value criteria-builder UI used by the Playlist
// Filter, Playlist Cleanup, Cascade, and Most Played pages. Field/operator
// validity comes from the backend (playlists/playlist_filter.py
// FIELD_OPERATORS, extended in playlists/playlist_cleanup.py; or
// lastfm/most_played.py's own FIELD_OPERATORS) via fieldOperatorsFromSpec;
// only display labels live here.

const FIELD_LABELS = {
    added_date: "Added date",
    release_year: "Release year",
    popularity: "Popularity (0-100)",
    explicit: "Explicit",
    artist: "Artist name",
    track_name: "Track name",
    genre: "Genre",
    playcount: "Play count",
    followers: "Followers",
    album_name: "Album name",
    artist_name: "Artist name",
};

const OPERATOR_LABELS = {
    within_last: "was added within the last",
    older_than: "was added more than … ago",
    is: "is",
    before: "is before",
    after: "is after",
    between: "is between",
    at_least: "is at least",
    at_most: "is at most",
    contains: "contains",
};

const UNIT_LABELS = { days: "day(s)", weeks: "week(s)", months: "month(s)" };

// Converts the backend's {field: [operator_key, ...]} spec into the
// {field: [{value, label}, ...]} shape the builder needs. Field display
// order follows the object's key order, which matches the Python dict's
// insertion order.
function fieldOperatorsFromSpec(spec) {
    const result = {};
    Object.keys(spec).forEach((field) => {
        result[field] = spec[field].map((op) => ({
            value: op,
            label: OPERATOR_LABELS[op],
        }));
    });
    return result;
}

function describeCriterion(criterion) {
    if (criterion.field === "added_date") {
        const opLabel =
            criterion.operator === "within_last"
                ? "was added within the last"
                : "was added more than";
        const suffix = criterion.operator === "within_last" ? "" : " ago";
        return (
            opLabel +
            " " +
            criterion.value +
            " " +
            (UNIT_LABELS[criterion.value2] || criterion.value2) +
            suffix
        );
    }
    let desc =
        FIELD_LABELS[criterion.field] +
        " " +
        OPERATOR_LABELS[criterion.operator] +
        " ";
    if (criterion.field === "explicit") {
        desc += criterion.value === "yes" ? "yes" : "no";
    } else if (criterion.operator === "between") {
        desc += criterion.value + " and " + criterion.value2;
    } else {
        desc += '"' + criterion.value + '"';
    }
    return desc;
}

function describeCriteria(criteria) {
    return criteria.map(describeCriterion).join(" and ");
}

// Builds one field/operator/value condition row into `container`.
// fieldOperators: {field: [{value, label}, ...]} (see
// fieldOperatorsFromSpec). fieldOrder: [field, ...] display order for the
// field <select>. Returns { row, getValues() }; getValues() throws an
// Error with a readable message if the condition isn't complete.
function buildCriteriaBuilder(container, fieldOperators, fieldOrder, initial) {
    const row = document.createElement("div");
    row.className = "criteria-row";

    const fieldSelect = document.createElement("select");
    fieldOrder.forEach((field) => {
        const opt = document.createElement("option");
        opt.value = field;
        opt.textContent = FIELD_LABELS[field];
        if (initial && field === initial.field) opt.selected = true;
        fieldSelect.appendChild(opt);
    });
    const operatorSelect = document.createElement("select");
    const valueInputs = document.createElement("span");
    row.append(fieldSelect, operatorSelect, valueInputs);
    container.appendChild(row);

    function updateOperators(selectedOperator) {
        const field = fieldSelect.value;
        operatorSelect.innerHTML = "";
        fieldOperators[field].forEach((op) => {
            const opt = document.createElement("option");
            opt.value = op.value;
            opt.textContent = op.label;
            if (op.value === selectedOperator) opt.selected = true;
            operatorSelect.appendChild(opt);
        });
        updateValueInputs();
    }

    function updateValueInputs(value, value2) {
        const field = fieldSelect.value;
        const operator = operatorSelect.value;
        valueInputs.innerHTML = "";

        if (field === "added_date") {
            const input1 = document.createElement("input");
            input1.type = "number";
            input1.min = "1";
            input1.value = value || "1";
            input1.style.width = "5rem";
            input1.className = "value-input";
            valueInputs.appendChild(input1);
            const select = document.createElement("select");
            select.className = "value-input-2";
            [
                ["days", "day(s)"],
                ["weeks", "week(s)"],
                ["months", "month(s)"],
            ].forEach(([v, label]) => {
                const opt = document.createElement("option");
                opt.value = v;
                opt.textContent = label;
                if (v === (value2 || "months")) opt.selected = true;
                select.appendChild(opt);
            });
            valueInputs.appendChild(select);
            return;
        }

        if (field === "explicit") {
            const select = document.createElement("select");
            select.className = "value-input";
            [
                ["yes", "Explicit"],
                ["no", "Not explicit"],
            ].forEach(([v, label]) => {
                const opt = document.createElement("option");
                opt.value = v;
                opt.textContent = label;
                if (v === value) opt.selected = true;
                select.appendChild(opt);
            });
            valueInputs.appendChild(select);
            return;
        }

        const numericFields = ["release_year", "popularity", "playcount", "followers"];
        const input1 = document.createElement("input");
        input1.type = numericFields.includes(field) ? "number" : "text";
        input1.className = "value-input";
        input1.placeholder =
            field === "release_year"
                ? "e.g. 2026"
                : field === "popularity"
                  ? "0-100"
                  : field === "playcount" || field === "followers"
                    ? "e.g. 50"
                    : "text to match";
        if (value) input1.value = value;
        valueInputs.appendChild(input1);

        if (field === "release_year" && operator === "between") {
            const and = document.createElement("span");
            and.textContent = " and ";
            valueInputs.appendChild(and);
            const input2 = document.createElement("input");
            input2.type = "number";
            input2.className = "value-input-2";
            input2.placeholder = "e.g. 2027";
            if (value2) input2.value = value2;
            valueInputs.appendChild(input2);
        }
    }

    fieldSelect.addEventListener("change", () => updateOperators());
    operatorSelect.addEventListener("change", () => updateValueInputs());
    if (initial) {
        updateOperators(initial.operator);
        updateValueInputs(initial.value, initial.value2);
    } else {
        updateOperators();
    }

    return {
        row,
        getValues() {
            const field = fieldSelect.value;
            const operator = operatorSelect.value;
            const valueEl = valueInputs.querySelector(".value-input");
            const value2El = valueInputs.querySelector(".value-input-2");
            const value = valueEl ? String(valueEl.value).trim() : "";
            const value2 = value2El ? String(value2El.value).trim() : "";
            if (!value) throw new Error("Enter a value for every condition.");
            if (value2El && !value2)
                throw new Error("Fill in both parts of every condition.");
            return { field, operator, value, value2: value2 || null };
        },
    };
}

// Wraps buildCriteriaBuilder to manage a variable-length, AND-combined list
// of condition rows (used by the Playlist Filter step; Playlist Cleanup
// uses a single condition via buildCriteriaBuilder directly). Follows the
// same addRow/reset/getValues shape as createComboRowManager in
// playlist_picker.js - the caller wires its own "+ Add" button to addRow().
function buildCriteriaListBuilder(containerEl, fieldOperators, fieldOrder) {
    let rows = [];

    function updateRemoveButtons() {
        const disable = rows.length <= 1;
        rows.forEach((entry) => {
            entry.removeBtn.disabled = disable;
        });
    }

    function addRow(initial) {
        const wrap = document.createElement("div");
        wrap.className = "criteria-list-row";
        const rowSlot = document.createElement("span");
        rowSlot.style.flex = "1";
        wrap.appendChild(rowSlot);
        const criteria = buildCriteriaBuilder(
            rowSlot,
            fieldOperators,
            fieldOrder,
            initial,
        );

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "button secondary remove-slot-btn";
        removeBtn.textContent = "×";
        wrap.appendChild(removeBtn);

        containerEl.appendChild(wrap);

        const entry = { wrap, criteria, removeBtn };
        removeBtn.addEventListener("click", () => removeRow(entry));
        rows.push(entry);
        updateRemoveButtons();
    }

    function removeRow(entry) {
        if (rows.length <= 1) return;
        rows = rows.filter((e) => e !== entry);
        entry.wrap.remove();
        updateRemoveButtons();
    }

    function reset(initialValues) {
        containerEl.innerHTML = "";
        rows = [];
        (initialValues && initialValues.length
            ? initialValues
            : [null]
        ).forEach((v) => addRow(v));
    }

    return {
        addRow,
        reset,
        getValues: () => rows.map((entry) => entry.criteria.getValues()),
    };
}
