from setuptools import setup
from os import path

with open('README.md') as f:
    long_description = f.read()

name = 'rtsp'
version = '1.0.11'

### include README as main package docfile
from shutil import copyfile
_workdir = path.abspath(path.dirname(__file__))
copyfile(_workdir+'/README.md',_workdir+'/{0}/__doc__'.format(name))

setup(name=name
    , version=version
    , description='RTSP client'
    , long_description=long_description
    , long_description_content_type='text/markdown'
    , author = 'Michael Stewart'
    , author_email = 'statueofmike@gmail.com'
    , url='https://github.com/statueofmike/rtsp'
    , download_url="https://github.com/statueofmike/rtsp/archive/{0}.tar.gz".format(version)
    , license='MIT'
    , packages=['rtsp']
    , include_package_data=True     # includes files from e.g. MANIFEST.in
    , classifiers=[
        'Development Status :: 5 - Production/Stable',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Topic :: Multimedia :: Video',
        'Topic :: Multimedia :: Video :: Capture',
        'Topic :: System :: Networking'
      ]
    , keywords='rtsp image stream'
    , install_requires=['pillow','opencv-python']
    , python_requires='>=3.5'
    , zip_safe=False
      )


