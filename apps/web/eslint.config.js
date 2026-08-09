// ESLint flat config for the web frontend.
//
// Deliberately the API's config plus what a React codebase needs: the same
// type-aware base, the same unused-vars and type-import rules, and the React
// Hooks plugin on top. Keeping the two configs recognisably the same means a
// contributor moving between packages does not relearn the rules.
//
// Formatting is not enforced here either; there is still no Prettier in the
// repository.
//
// Not installed: eslint-plugin-jsx-a11y. Its current release declares peer
// support only up to ESLint 9, and this repository is on ESLint 10 — adding it
// would mean either a forced peer resolution or downgrading ESLint for one
// package. The accessibility requirements (4.6) are covered by Testing Library
// assertions on roles, labels, and descriptions in spec tasks 5.2 to 5.4, which
// check the rendered output rather than the source.

import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: ['dist/**', 'node_modules/**', 'coverage/**'],
  },

  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  // `configs.flat` is the flat-config namespace of the plugin; the top-level
  // `configs.recommended` is still the legacy eslintrc shape and ESLint 10
  // rejects it.
  reactHooks.configs.flat['recommended-latest'],

  {
    files: ['**/*.ts', '**/*.tsx'],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
        },
      ],
      '@typescript-eslint/consistent-type-imports': [
        'error',
        { prefer: 'type-imports', fixStyle: 'separate-type-imports' },
      ],
      // A dropped promise in a submit handler is a form that silently never
      // finishes.
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/no-misused-promises': 'error',
      '@typescript-eslint/no-explicit-any': 'error',
      eqeqeq: ['error', 'always', { null: 'ignore' }],
      // Allowed only with an explicit disable comment naming the reason, as in
      // the boot-time configuration warning in main.tsx.
      'no-console': 'error',
    },
  },

  {
    files: ['**/*.test.ts', '**/*.test.tsx', '**/testing/**/*.ts', '**/testing/**/*.tsx'],
    rules: {
      '@typescript-eslint/no-unsafe-assignment': 'off',
      '@typescript-eslint/no-unsafe-argument': 'off',
      '@typescript-eslint/no-unsafe-member-access': 'off',
      '@typescript-eslint/no-non-null-assertion': 'off',
    },
  },

  {
    // This config file and `public/config.js`: plain JS with no type information,
    // so type-aware rules must not run against them.
    files: ['**/*.js'],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: {
      globals: { window: 'readonly' },
    },
  },
);
