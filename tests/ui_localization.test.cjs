'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '../voice_lab_i18n.js'), 'utf8');
function helper(language, messages) {
  const english = {
    greeting: 'Welcome, {name}',
    stock: {one: '{count} voice for {play}', other: '{count} voices for {play}'},
    fallback: {one: '{count} line', other: '{count} lines'},
    'error.model_not_installed': 'Install the model.',
  };
  const config = {language, english, messages};
  const context = vm.createContext({
    document: {
      getElementById: () => ({textContent: JSON.stringify(config)}),
      addEventListener() {},
    },
  });
  vm.runInContext(source, context);
  return context.LabText;
}

test('named places can move and values are substituted only once', () => {
  const words = helper('es', {greeting: '{name}, te damos la bienvenida'});
  assert.equal(words.t('greeting', {name: '{other}', other: 'replace me'}), '{other}, te damos la bienvenida');
  assert.equal(words.t('greeting', {name: 'Harbor'}), 'Harbor, te damos la bienvenida');
});

test('plurals use the selected language and missing messages use English rules', () => {
  const words = helper('ru', {
    stock: {one: '{count} голос', few: '{count} голоса', many: '{count} голосов', other: '{count} голоса'},
  });
  assert.equal(words.plural('stock', 1), '1 голос');
  assert.equal(words.plural('stock', 3), '3 голоса');
  assert.equal(words.plural('stock', 7), '7 голосов');
  assert.equal(words.plural('fallback', 21), '21 lines');
  assert.equal(words.t('greeting', {name: 'Mira'}), 'Welcome, Mira');
  assert.equal(words.t('added.later'), 'added.later');
  assert.equal(helper('en', {}).plural('stock', 0, {play: 'Harbor'}), '0 voices for Harbor');
});

test('translated common errors keep an actionable sentence and use service text for missing translations', () => {
  const error = {error: 'Install Kokoro 82M under Models.', code: 'model_not_installed'};
  assert.equal(helper('en', {}).error(error, 503), error.error);
  assert.equal(helper('es', {}).error(error, 503), error.error);
  assert.equal(helper('es', {'error.model_not_installed': 'Instala el modelo.'}).error(error, 503), 'Instala el modelo.');
  assert.equal(helper('es', {}).error({}, 500), 500);
});
