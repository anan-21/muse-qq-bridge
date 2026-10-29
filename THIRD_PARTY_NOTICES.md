# Third-party software

The QQ gateway integration uses [Tencent botpy](https://github.com/tencent-connect/botpy),
installed as `qq-botpy`, under the MIT License. Tencent SDK HTTP request and client
integration patterns are adapted in `adapter.py`. The dependency is installed
separately; its full distribution is not copied into this repository.

Tencent's applicable license notice follows:

MIT License

Copyright (c) 2021 Tencent

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

`aiohttp` and the SDK's other dependencies retain their own distribution
licenses. This repository does not contain the Muse app, a model implementation,
Tencent QQ, or Tailscale. No affiliation with their vendors is implied.
