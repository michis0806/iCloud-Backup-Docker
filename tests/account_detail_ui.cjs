// Offline component regression checks: node tests/account_detail_ui.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const { test } = require('node:test');

const template = fs.readFileSync(path.join(__dirname, '../app/templates/account_detail.html'), 'utf8');
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];

function component() {
    let timers = 0;
    const context = vm.createContext({
        console,
        setInterval: () => ++timers,
        clearInterval: () => {},
    });
    vm.runInContext(script, context);
    return { context, state: context.accountConfig('test@example.com'), timers: () => timers };
}

test('Alpine initialization is not also called by x-init', () => {
    assert.doesNotMatch(template, /x-init="init\(\)"/);
    assert.match(template, /@click="submitReauth\(true\)"/);
});

for (const status of ['error', 'requires_2fa', 'authenticated']) {
    test(`opening ${status} account does not start reauth`, async () => {
        const { state, timers } = component();
        state.loadAccount = async () => { state.account = { status }; };
        for (const method of ['loadConfig', 'loadBackupStatus', 'loadDriveFolders', 'loadPhotoLibraries']) {
            state[method] = async () => {};
        }
        state.submitReauth = async () => assert.fail('Unexpected reauth');
        await state.init();
        assert.equal(timers(), 1);
    });
}

for (const result of [
    { valid: false, requires_2fa: true, message: 'Code needed' },
    { valid: false, requires_password: true, message: 'Password needed' },
    { valid: true, requires_2fa: false, message: 'Connected' },
]) {
    test(`connection check displays result without opening reauth: ${result.message}`, async () => {
        const { context, state } = component();
        const calls = [];
        context.fetch = async (url, options) => {
            calls.push([url, options.method]);
            return { json: async () => result };
        };
        state.loadAccount = async () => {};
        state.submitReauth = async () => assert.fail('Unexpected reauth');
        await state.checkConnection();
        assert.equal(state.connectionResult, result);
        assert.equal(state.showReauthForm, false);
        assert.equal(state.checkingConnection, false);
        assert.equal(calls.length, 1);
        assert.match(calls[0][0], /\/check-connection$/);
    });
}
