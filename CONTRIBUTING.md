# Contributing to EasyTakeout

Thank you for your interest in contributing to EasyTakeout! This document provides guidelines and information for contributors.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [How to Contribute](#how-to-contribute)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Documentation](#documentation)
- [Pull Request Process](#pull-request-process)
- [Issue Reporting](#issue-reporting)
- [Community](#community)

## Code of Conduct

This project and everyone participating in it is governed by our [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

## Getting Started

### Prerequisites

- Python 3.8 or higher
- Git
- Basic familiarity with PySide6/Qt for GUI development
- Understanding of Google Takeout data structure

### First Steps

1. Fork the repository on GitHub
2. Clone your fork locally
3. Set up the development environment
4. Make your changes
5. Test your changes
6. Submit a pull request

## Development Setup

### 1. Clone the Repository

```bash
git clone https://github.com/yourusername/EasyTakeout.git
cd EasyTakeout
```

### 2. Create a Virtual Environment

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt  # Development dependencies
```

### 4. Install Pre-commit Hooks

```bash
pre-commit install
```

### 5. Run Tests

```bash
pytest tests/ -v
```

### 6. Run the Application

```bash
python app/TakeoutMetadataMergerApp.py
```

## How to Contribute

### Types of Contributions

We welcome several types of contributions:

- **Bug Reports**: Help us identify and fix issues
- **Feature Requests**: Suggest new functionality
- **Code Contributions**: Fix bugs or implement features
- **Documentation**: Improve docs, guides, and examples
- **Testing**: Add test cases or improve test coverage
- **Translations**: Help make the app available in more languages

### Finding Work

- Check the [issue tracker](https://github.com/yourusername/EasyTakeout/issues)
- Look for issues labeled `good first issue` for newcomers
- Issues labeled `help wanted` are specifically looking for contributors
- Check the [roadmap](docs/ROADMAP.md) for planned features

## Coding Standards

### Python Style

We follow PEP 8 with some modifications:

- Line length: 88 characters (Black default)
- Use Black for code formatting
- Use isort for import sorting
- Use flake8 for linting
- Use mypy for type checking

### Code Quality Tools

Before submitting code, ensure it passes:

```bash
# Format code
black app/ cli/ tests/
isort app/ cli/ tests/

# Lint code
flake8 app/ cli/ tests/

# Type checking
mypy app/ cli/
```

### Git Workflow

1. Create a feature branch from `main`
2. Make your changes in logical commits
3. Write descriptive commit messages
4. Push to your fork
5. Create a pull request

### Commit Message Format

```
<type>(<scope>): <description>

<body>

<footer>
```

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Adding or modifying tests
- `chore`: Maintenance tasks

Example:
```
feat(gui): add dark mode toggle

- Add dark mode option to settings menu
- Implement theme switching functionality
- Update all UI components to support dark theme

Closes #123
```

### Code Organization

- **app/**: Main application code
- **cli/**: Command-line interface
- **tests/**: Test files
- **docs/**: Documentation
- **packaging/**: Build and packaging scripts

### Naming Conventions

- Classes: `PascalCase`
- Functions/variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Files: `snake_case.py`
- Private methods: `_leading_underscore`

## Testing

### Test Structure

- Unit tests for individual functions/classes
- Integration tests for component interactions
- GUI tests for user interface components
- End-to-end tests for complete workflows

### Running Tests

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_json_mapping.py

# Run with coverage
pytest --cov=app --cov=cli tests/

# Run GUI tests (requires display)
pytest tests/test_gui.py -v
```

### Writing Tests

- Write tests for new functionality
- Aim for >80% code coverage
- Use descriptive test names
- Include both positive and negative test cases
- Mock external dependencies

## Documentation

### Documentation Types

- **Code Documentation**: Docstrings for classes and functions
- **User Documentation**: User guides and tutorials
- **Developer Documentation**: Architecture and design docs
- **API Documentation**: If/when API is added

### Writing Documentation

- Use clear, concise language
- Include examples where helpful
- Keep documentation up-to-date with code changes
- Use proper Markdown formatting

## Pull Request Process

### Before Submitting

1. Ensure all tests pass
2. Update documentation if needed
3. Add yourself to AUTHORS.md (if not already there)
4. Update CHANGELOG.md with your changes

### Pull Request Description

Include:
- Clear description of changes
- Reference to related issues
- Screenshots (for UI changes)
- Testing performed
- Breaking changes (if any)

### Review Process

1. Automated checks must pass (CI/CD)
2. Code review by maintainers
3. Address feedback if requested
4. Approval and merge

### Merge Requirements

- All CI checks pass
- At least one approving review
- No requested changes
- Branch is up-to-date with main

## Issue Reporting

### Bug Reports

Use the bug report template and include:
- Clear description of the problem
- Steps to reproduce
- Expected vs actual behavior
- Environment information
- Screenshots/logs if applicable

### Feature Requests

Use the feature request template and include:
- Problem being solved
- Proposed solution
- Alternative approaches considered
- Use cases and benefits

### Security Issues

For security vulnerabilities:
- **Do not** create a public issue
- Email security@easytakeout.com
- Include detailed description
- Wait for response before disclosure

## Community

### Communication Channels

- **GitHub Issues**: Bug reports and feature requests
- **GitHub Discussions**: General questions and ideas
- **Email**: security@easytakeout.com for security issues

### Getting Help

- Check existing documentation
- Search existing issues and discussions
- Ask questions in GitHub Discussions
- Be respectful and patient

### Recognition

Contributors are recognized in:
- AUTHORS.md file
- Release notes for significant contributions
- GitHub contributors page

## Development Resources

### Useful Links

- [PySide6 Documentation](https://doc.qt.io/qtforpython/)
- [Google Takeout Format](https://support.google.com/accounts/answer/3024190)
- [Python Testing with pytest](https://docs.pytest.org/)
- [Git Workflow](https://guides.github.com/introduction/flow/)

### Project Architecture

```
EasyTakeout/
├── app/                    # Main application
│   └── TakeoutMetadataMergerApp.py
├── cli/                    # Command-line interface
├── tests/                  # Test suite
├── docs/                   # Documentation
└── packaging/              # Build scripts
```

### Key Components

- **GUI Framework**: PySide6 (Qt for Python)
- **File Processing**: Custom JSON/metadata handling
- **Testing**: pytest with Qt testing support
- **Building**: PyInstaller for executables
- **CI/CD**: GitHub Actions

## Thank You

Your contributions help make EasyTakeout better for everyone. Whether you're fixing bugs, adding features, improving documentation, or helping other users, your efforts are appreciated!

For questions about contributing, feel free to open a discussion or contact the maintainers.
