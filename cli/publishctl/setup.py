"""PublishHub developer CLI — publishctl.

Thin wrapper around make, kubectl, and helm that provides a single entry point
for common platform operations.
"""

from setuptools import setup, find_packages

setup(
    name="publishctl",
    version="0.1.0",
    description="PublishHub developer CLI",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "click==8.1.7",
        "rich==13.7.1",
    ],
    entry_points={
        "console_scripts": [
            "publishctl=publishctl.cli:cli",
        ],
    },
)
