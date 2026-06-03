from setuptools import setup
from os import path

with open('README.md') as f:
    long_description = ''.join(
        line for line in f if not line.strip().startswith('[![')
    )

name = 'rtsp'
version = '2.0.0'

### include README as main package docfile
_workdir = path.abspath(path.dirname(__file__))
with open(_workdir+'/{0}/__doc__'.format(name), 'w') as f:
    f.write(long_description)

setup(name=name
    , version=version
    , description='RTSP/RTMP client and server for Python.'
    , long_description=long_description
    , long_description_content_type='text/markdown'
    , author = 'Cory Root'
    , url='https://github.com/dactylroot/rtsp'
    , download_url="https://github.com/dactylroot/rtsp/archive/{0}.tar.gz".format(version)
    , license='MIT'
    , packages=['rtsp']
    , include_package_data=True     # includes files from e.g. MANIFEST.in
    , classifiers=[
        'Development Status :: 5 - Production/Stable',
        'License :: OSI Approved :: MIT License',
        'Intended Audience :: Developers',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3 :: Only',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
        'Programming Language :: Python :: 3.14',
        'Topic :: Multimedia :: Video',
        'Topic :: Multimedia :: Video :: Capture',
        'Topic :: Multimedia :: Video :: Display',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Topic :: System :: Networking'
      ]
    , keywords='rtsp rtmp image stream server numpy pillow'
    , install_requires=['pillow', 'numpy', 'av']
    , python_requires='>=3.10'
    , zip_safe=False
      )
