Unit Tests now detect and run test frameworks automatically when a repository
has the unit-tests activity but has never saved Unit Testing settings (no
unitTests block in .github/labview-ci.yml). Every framework found runs: NI UTF
for any .lvproj that contains .lvtest items, and Caraya, LUnit or VI Tester for
directories holding VIs that link Caraya.lvlib, LUnit.lvlib or TestCase.lvclass.
Previously such repositories produced an empty "no tests" report. An explicit
unitTests block, even with every framework disabled, is still honoured as-is.
