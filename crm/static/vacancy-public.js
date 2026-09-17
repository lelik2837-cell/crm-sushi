(() => {
    // Only the chosen vacancy's questions are rendered, so hidden required
    // fields from other vacancies cannot block submission.
    document.querySelectorAll('[data-branch-group][data-required]').forEach(group => {
        const inputs = [...group.querySelectorAll('input[type="checkbox"]')];
        const update = () => {
            inputs[0]?.setCustomValidity(inputs.some(input => input.checked) ? '' : 'Выберите хотя бы один филиал');
        };
        group.addEventListener('change', update);
        update();
    });
    // The host snippet checks both source and origin before resizing its iframe.
    if (window.parent !== window) {
        let previousHeight = 0;
        const reportHeight = (force = false) => {
            const height = Math.ceil(document.getElementById('careers').getBoundingClientRect().height);
            if (force || height !== previousHeight) {
                window.parent.postMessage({type: 'papa-vacancy-height', height}, '*');
                previousHeight = height;
            }
        };
        new ResizeObserver(() => reportHeight()).observe(document.getElementById('careers'));
        window.addEventListener('message', event => {
            if (event.source === window.parent && event.data?.type === 'papa-vacancy-measure') reportHeight(true);
        });
        window.addEventListener('load', reportHeight);
        reportHeight();
    }
})();
