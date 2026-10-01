/* iCloud Backup Service – Global JS utilities */

// Alpine.js components are defined inline. window.I18N is rendered by base.html.
function t(key, params) {
    let text = (window.I18N && window.I18N[key]) || key;
    if (params) {
        text = text.replace(/\{(\w+)\}/g, (m, name) => (name in params ? params[name] : m));
    }
    return text;
}
