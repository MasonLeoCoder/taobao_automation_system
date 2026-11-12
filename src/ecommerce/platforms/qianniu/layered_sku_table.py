"""从千牛规格表的表头、合并单元格和固定列读取可核对的行。"""

FIXED_TABLE = '.next-table-fixed, .next-table-fixed-left, .next-table-fixed-right'

READ_LAYERED_TABLE = """(rows, {labels, expectedPairs}) => {
    const visible = element => element.getClientRects().length &&
        getComputedStyle(element).visibility === 'visible';
    const text = value => (value || '').replace(/\\s+/g, ' ').trim();
    const tables = new Map();
    rows.forEach((row, index) => {
        const table = row.closest('table');
        if (!table || !visible(row)) return;
        if (!tables.has(table)) tables.set(table, []);
        tables.get(table).push({row, index});
    });
    if (!tables.size) return {error: '千牛没有可读取的分层 SKU 行'};

    function grid(rows) {
        const matrix = Array.from(rows, () => []);
        rows.forEach((row, index) => {
            let column = 0;
            for (const cell of row.cells) {
                while (matrix[index][column]) column++;
                const end = cell.rowSpan ? Math.min(index + cell.rowSpan, rows.length) : rows.length;
                for (let line = index; line < end; line++) {
                    for (let offset = 0; offset < cell.colSpan; offset++) {
                        matrix[line][column + offset] = cell;
                    }
                }
                column += cell.colSpan;
            }
        });
        return matrix;
    }

    function bodyCells(entries) {
        const sections = new Map();
        return entries.map(({row}) => {
            const section = row.parentElement;
            if (!sections.has(section)) {
                const physicalRows = Array.from(section.rows);
                const cells = grid(physicalRows);
                sections.set(section, new Map(physicalRows.map((item, index) => [item, cells[index]])));
            }
            return sections.get(section).get(row);
        });
    }

    const headerNames = cell => [cell.innerText, cell.getAttribute('title'), cell.getAttribute('aria-label'),
        ...Array.from(cell.querySelectorAll('[title]'), child => child.title)].map(text);

    function columnIndexes(head, width) {
        const cells = grid(Array.from(head.rows));
        if (!cells.length) return null;
        const headerWidth = Math.max(...cells.map(row => row.length));
        const gutter = cells.at(-1)[width];
        if (headerWidth !== width && !(headerWidth === width + 1 && gutter &&
            /scrollbar|scroll-bar|scroll-gutter/i.test(gutter.className) && !text(gutter.innerText))) return null;
        const candidates = labels.map(label => new Set(cells.flatMap(row => row.flatMap((cell, index) => {
            return cell.colSpan === 1 && headerNames(cell).includes(label) ? [index] : [];
        }))));
        if (!candidates.every(values => values.size === 1)) return null;
        const indexes = candidates.map(values => [...values][0]);
        return indexes[0] !== indexes[1] ? indexes : null;
    }

    function columnsFromValues(cells, width) {
        // 千牛可能将表头独立渲染。此时只接受与 Excel 全部组合精确一致的唯一列对。
        if (cells.length !== expectedPairs.length) return null;
        const expected = new Set(expectedPairs.map(pair => JSON.stringify(pair)));
        if (expected.size !== expectedPairs.length) return null;
        const allowed = labels.map((_, dimension) => new Set(expectedPairs.map(pair => pair[dimension])));
        const values = Array.from({length: width}, (_, index) => cells.map(row => {
            const cell = row[index];
            return cell && visible(cell) && !cell.querySelector('input, textarea, select, [contenteditable="true"]')
                ? text(cell.innerText) : '';
        }));
        const candidates = allowed.map(valid => values.map((column, index) =>
            column.every(value => valid.has(value)) ? index : -1).filter(index => index >= 0));
        const matches = [];
        for (const first of candidates[0]) {
            for (const second of candidates[1]) {
                if (first === second) continue;
                const observed = new Set(values[first].map((color, index) => JSON.stringify([color, values[second][index]])));
                if (observed.size === expected.size && [...observed].every(pair => expected.has(pair))) {
                    matches.push([first, second]);
                }
            }
        }
        return matches.length === 1 ? matches[0] : null;
    }

    function readPairs(table, entries, group) {
        const cells = bodyCells(entries);
        const width = Math.max(...cells.map(row => row.length));
        let heads = [];
        let indexes = table.tHead && columnIndexes(table.tHead, width);
        if (!indexes) {
            for (let area = table.parentElement; area && group.contains(area); area = area.parentElement) {
                heads = Array.from(area.querySelectorAll('thead')).filter(visible);
                const candidates = heads.map(head => columnIndexes(head, width)).filter(Boolean);
                const layouts = new Set(candidates.map(candidate => candidate.join(',')));
                if (layouts.size > 1) return {error: '千牛有多套不同的颜色和尺码表头，无法确认列位置'};
                if (candidates.length) {
                    [indexes] = candidates;
                    break;
                }
            }
        }
        indexes ||= columnsFromValues(cells, width);
        if (!indexes) {
            const displayed = new Set(heads.flatMap(head => Array.from(head.querySelectorAll('th,td')).flatMap(headerNames)));
            const missing = labels.filter(label => !displayed.has(label));
            return {error: missing.length
                ? '千牛规格表没有读到表头「' + missing.join('、') + '」'
                : '无法将千牛表头的「颜色分类」和「适用尺码」与数据列对应',
                diagnostics: {
                    rowCount: entries.length, expectedCount: expectedPairs.length, bodyColumns: width,
                    tableClasses: [...table.classList], containerClasses: [...table.parentElement.classList],
                    nativeHeads: heads.length, gridHeaders: group.querySelectorAll('[role="columnheader"]').length,
                    mergedRows: cells.some(row => row.some(cell => cell.rowSpan > 1)),
                }};
        }
        const pairs = cells.map(row => indexes.map(index => text(row[index]?.innerText)));
        const blank = pairs.findIndex(pair => pair.some(value => !value));
        if (blank >= 0) {
            const label = labels[pairs[blank].findIndex(value => !value)];
            return {error: '千牛规格表第 ' + (blank + 1) + ' 行的「' + label + '」没有读到值'};
        }
        return {pairs};
    }

    const group = rows[0].closest('#sell-field-sku');
    if (!group) return {error: '无法定位千牛销售规格区域'};
    const editable = [...tables].filter(([, entries]) => entries.every(({row}) =>
        ['skuPrice', 'skuStock', 'skuOuterId'].every(field => row.querySelector('td[id$="-' + field + '"] input'))));
    const mainTables = editable.filter(([table]) => !table.closest('__FIXED_TABLE__'));
    const candidates = mainTables.length ? mainTables : editable;
    if (candidates.length !== 1 && tables.size !== 1) {
        return {error: '千牛规格表有多套数据行，无法唯一确认可编辑行'};
    }
    const [table, entries] = candidates[0] || [...tables][0];
    let result = readPairs(table, entries, group);
    if (!result.pairs && tables.size > 1) {
        // 固定列与编辑列分属不同表体时，仅通过相同的明确行标识合并。
        const key = row => row.getAttribute('data-row-key') || row.getAttribute('data-sku-id') ||
            row.getAttribute('data-key') || row.id;
        const expected = entries.map(({row}) => key(row));
        if (expected.every(Boolean) && new Set(expected).size === expected.length) {
            const matching = [];
            for (const [other, source] of tables) {
                if (other === table || source.length !== entries.length) continue;
                const observed = readPairs(other, source, group);
                if (!observed.pairs) continue;
                const byKey = new Map(source.map(({row}, index) => [key(row), observed.pairs[index]]));
                if (expected.every(value => byKey.has(value))) matching.push(expected.map(value => byKey.get(value)));
            }
            if (matching.length && new Set(matching.map(pairs => JSON.stringify(pairs))).size === 1) result = {pairs: matching[0]};
        }
    }
    if (!result.pairs) return result;
    return {fixed: Boolean(table.closest('__FIXED_TABLE__')), entries: entries.map(({row, index}, position) => ({
        index, pair: result.pairs[position], codeId: row.querySelector('td[id$="-skuOuterId"]')?.id || null,
    }))};
}""".replace('__FIXED_TABLE__', FIXED_TABLE)
