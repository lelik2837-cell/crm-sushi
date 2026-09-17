(() => {
    const frame = document.currentScript?.previousElementSibling;
    if (!frame || frame.tagName !== 'IFRAME') return;
    const origin = new URL(frame.src, location.href).origin;
    window.addEventListener('message', event => {
        if (event.source !== frame.contentWindow || event.origin !== origin) return;
        if (event.data?.type !== 'papa-vacancy-height') return;
        const height = Number(event.data.height);
        if (!Number.isFinite(height) || height < 1 || height > 20000) return;
        frame.style.height = Math.max(240, Math.ceil(height)) + 'px';
    });
    const measure = () => frame.contentWindow.postMessage({type:'papa-vacancy-measure'}, origin);
    frame.addEventListener('load', measure);
    measure();
})();
