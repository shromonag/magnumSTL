from setuptools import setup, find_packages

setup(name='blustl',
      version='0.1',
      description='TODO',
      url='http://github.com/mvcisback/py-blustl',
      author='Marcell Vazquez-Chanlatte',
      author_email='marcell.vc@eecs.berkeley.edu',
      license='MIT',
      install_requires=[
          'funcy',
          'parsimonious', 
          'PyYAML',
          'singledispatch',
          'enum34',
          'numpy',
          'nose2',
      ],
      packages=find_packages(),
)