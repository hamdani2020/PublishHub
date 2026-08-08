// ESLint flat config for the API service.
//
// Type-aware linting is on: the rules that catch the mistakes worth catching in
// an async service (floating promises, misused promises, unnecessary awaits)
// need type information, and the package already runs `tsc` in CI so the extra
// cost is small. `projectService` resolves each linted file to the nearest
// tsconfig, which for `src/**` is `tsconfig.json`.
//
// Formatting is deliberately not enforced here. There is no Prettier in the
// repository yet, and lint rules that argue about whitespace add noise without
// catching bugs.

import js from '@eslint/js';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    // Build output and dependencies are never linted.
    ignores: ['dist/**', 'node_modules/**', 'coverage/**'],
  },

  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,

  {
    files: ['**/*.ts'],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // Unused values are an error, but an underscore prefix is the documented
      // way to say "required by the signature, intentionally unused" — which
      // Express error handlers and interface implementations both need.
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
        },
      ],
      // Prefer the type-only import form; `verbatimModuleSyntax` in tsconfig
      // means the emitted JS depends on getting this right.
      '@typescript-eslint/consistent-type-imports': [
        'error',
        { prefer: 'type-imports', fixStyle: 'separate-type-imports' },
      ],
      // A dropped promise in a request handler is a silent 500 or a lost job.
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/no-misused-promises': 'error',
      // Off by design: the queue abstraction is an async interface, and several
      // implementations (the fakes, and the synchronous parts of the AWS port)
      // satisfy it without awaiting anything. Dropping `async` there would
      // change the declared signature to match an implementation detail.
      '@typescript-eslint/require-await': 'off',
      // `any` is allowed only with an explicit disable comment naming the reason.
      '@typescript-eslint/no-explicit-any': 'error',
      eqeqeq: ['error', 'always', { null: 'ignore' }],
      'no-console': 'error',
    },
  },

  {
    // Test files and fakes: `no-console` still applies, but assertions on
    // deliberately wrong shapes need looser type rules.
    files: ['**/*.test.ts', '**/testing/**/*.ts'],
    rules: {
      '@typescript-eslint/no-unsafe-assignment': 'off',
      '@typescript-eslint/no-unsafe-argument': 'off',
      '@typescript-eslint/no-unsafe-member-access': 'off',
      '@typescript-eslint/no-non-null-assertion': 'off',
    },
  },

  {
    // This config file itself, and any other plain JS: no type information is
    // available for it, so type-aware rules must not run.
    files: ['**/*.js'],
    extends: [tseslint.configs.disableTypeChecked],
  },
);
