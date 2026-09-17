(() => {
    const page = document.querySelector('.vacancy-page');
    if (!page) return;
    const storageKey = 'vacancies:return:' + page.dataset.tab + ':' +
        (page.dataset.tab === 'settings' ? page.dataset.section : 'applications');
    // Restore only after a form submission, not after switching sections.
    try {
        const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
        sessionStorage.removeItem(storageKey);
        if (saved) {
            (saved.open || []).forEach(id => {
                document.getElementById(id)?.classList.add('show');
                page.querySelector(`[aria-controls="${id}"]`)?.setAttribute('aria-expanded', 'true');
            });
            requestAnimationFrame(() => window.scrollTo(0, saved.scroll || 0));
        }
    } catch (_) { /* Navigation remains available without storage. */ }
    page.addEventListener('submit', event => {
        const form = event.target;
        if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
            event.preventDefault();
            return;
        }
        if (form.method.toLowerCase() !== 'post') return;
        try {
            sessionStorage.setItem(storageKey, JSON.stringify({
                scroll: window.scrollY,
                open: [...page.querySelectorAll('tr.collapse.show')].map(el => el.id),
            }));
        } catch (_) { /* Storage may be unavailable in private browsing. */ }
    });
    page.querySelectorAll('[data-field-type]').forEach(select => {
        const form = select.closest('form');
        const descriptions = {
            text: 'Одна строка — например, возраст или район проживания.',
            textarea: 'Несколько строк — удобно для рассказа об опыте работы.',
            select: 'Кандидат выберет один из вариантов, которые вы укажете ниже.',
            branches: 'Кандидат сможет отметить несколько действующих филиалов.',
        };
        const update = () => {
            const isSelect = select.value === 'select';
            form.querySelector('[data-field-options]').hidden = !isSelect;
            form.querySelector('[name="options"]').required = isSelect;
            form.querySelector('[data-type-hint]').textContent = descriptions[select.value];
        };
        select.addEventListener('change', update);
        update();
    });
    page.querySelectorAll('.modal').forEach(modal => {
        modal.addEventListener('shown.bs.modal', () => modal.querySelector('input:not([type="hidden"])')?.focus());
    });
    page.querySelectorAll('[data-copy]').forEach(button => {
        button.addEventListener('click', async () => {
            const input = document.getElementById(button.dataset.copy);
            const status = document.getElementById('vacancy-copy-status');
            try {
                await navigator.clipboard.writeText(input.value);
                status.textContent = 'Скопировано';
            } catch (_) {
                input.focus();
                input.select();
                status.textContent = 'Текст выделен. Нажмите Ctrl+C или ⌘C, чтобы скопировать.';
            }
        });
    });
})();
