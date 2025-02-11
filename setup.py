from setuptools import find_namespace_packages, setup

setup(
    name="hsem",
    version="1.0.0",
    packages=find_namespace_packages(include=["custom_components.*"]),
    include_package_data=True,
    zip_safe=False,
)
