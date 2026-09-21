import eslint from '@eslint/js'
import vue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**'] },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  ...vue.configs['flat/recommended'],
  {
    languageOptions: {
      globals: {
        window: 'readonly',
        localStorage: 'readonly',
        setTimeout: 'readonly',
        clearTimeout: 'readonly',
        // DOM types used by the Operations Console's form handlers. Declared
        // one by one, like the rest of this list, rather than switching the
        // whole config to `globals.browser` - the short list is a useful
        // record of what the frontend actually touches.
        Event: 'readonly',
        HTMLInputElement: 'readonly',
        HTMLSelectElement: 'readonly',
        document: 'readonly',
        navigator: 'readonly',
      },
    },
    rules: {
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/html-closing-bracket-spacing': 'off',
      'vue/html-self-closing': 'off',
    },
  },
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
        extraFileExtensions: ['.vue'],
      },
    },
    rules: {
      'vue/multi-word-component-names': 'off',
    },
  },
)
