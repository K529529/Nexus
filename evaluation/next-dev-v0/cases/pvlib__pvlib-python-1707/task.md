regression: iam.physical returns nan for aoi > 90° when n = 1
**Describe the bug**
For pvlib==0.9.5, when n = 1 (no reflection) and aoi > 90°, we get nan as result.

**To Reproduce**
```python
import pvlib
pvlib.iam.physical(aoi=100, n=1)
```
returns `nan`.

**Expected behavior**
The result should be `0`, as it was for pvlib <= 0.9.4.


**Versions:**
 - ``pvlib.__version__``: '0.9.5'
 - ``pandas.__version__``:  '1.5.3'
 - python: 3.10.4



Investigate the repository, implement the fix, and run relevant tests.
