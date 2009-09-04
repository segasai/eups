"""
The Eups class 
"""
import glob, re, os, pwd, shutil, sys, time
import filecmp
import fnmatch
import tempfile

from stack import ProductStack
from db import Database
from exceptions import ProductNotFound
from table import Table
import utils 
from utils import Flavor, Quiet

class Eups(object):
    """Control eups"""

    # static variable:  the name of the EUPS database directory inside a EUPS-
    #  managed software stack
    ups_db = "ups_db"

    def __init__(self, flavor=None, path=None, dbz=None, root=None, readCache=True,
                 shell=None, verbose=False, quiet=0,
                 noaction=False, force=False, ignore_versions=False, exact_version=False,
                 keep=False, max_depth=-1, preferredTag="current",
                 # above is the backward compatible signature
                 userDataDir=None
                 ):
                 
        self.verbose = verbose

        if not shell:
            try:
                shell = os.environ["SHELL"]
            except KeyError:
                raise RuntimeError, "I cannot guess what shell you're running as $SHELL isn't set"

            if re.search(r"(^|/)(bash|ksh|sh)$", shell):
                shell = "sh"
            elif re.search(r"(^|/)(csh|tcsh)$", shell):
                shell = "csh"
            elif re.search(r"(^|/)(zsh)$", shell):
                shell = "zsh"
            else:
                raise RuntimeError, ("Unknown shell type %s" % shell)    

        self.shell = shell

        if not flavor:
            flavor = getFlavor()
        self.flavor = flavor

        if not path:
            if os.environ.has_key("EUPS_PATH"):
                path = os.environ["EUPS_PATH"]
            else:
                path = []

        if isinstance(path, str):
            path = filter(lambda el: el, path.split(":"))
                
        if dbz:
            # if user provides dbz, restrict self.path to those
            # directories that start with dbz
            path = filter(lambda p: re.search(r"/%s(/|$)" % dbz, p), path)
            os.environ["EUPS_PATH"] = ":".join(path)

        self.path = []
        for p in path:
            if not os.path.isdir(p):
                print >> sys.stderr, \
                      "%s in $EUPS_PATH does not contain a ups_db directory, and is being ignored" % p
                continue

            self.path += [os.path.normpath(p)]

        if not self.path and not root:
            if dbz:
                raise RuntimeError, ("No element of EUPS_PATH matches \"%s\"" % dbz)
            else:
                raise RuntimeError, ("No EUPS_PATH is defined")

        self.oldEnviron = os.environ.copy() # the initial version of the environment

        self.aliases = {}               # aliases that we should set
        self.oldAliases = {}            # initial value of aliases.  This is a bit of a fake, as we
                                        # don't know how to set it but (un)?setAlias knows how to handle this

        self.who = re.sub(r",.*", "", pwd.getpwuid(os.getuid())[4])

        if root:
            root = re.sub(r"^~", os.environ["HOME"], root)
            if not os.path.isabs(root):
                root = os.path.join(os.getcwd(), root)
            root = os.path.normpath(root)
            
        self.root = root

        self.setCurrentType(currentType)
        self.quiet = quiet
        self.keep = keep
        self.alreadySetupProducts = {}  # used by setup() to remember what's setup
        self.noaction = noaction
        self.force = force
        self.ignore_versions = ignore_versions
        self.exact_version = exact_version
        self.max_depth = max_depth      # == 0 => only setup toplevel package

        self.locallyCurrent = {}        # products declared local only within self

        self._msgs = {}                 # used to suppress messages
        self._msgs["setup"] = {}        # used to suppress messages about setups

        # 
        # determine the user data directory.  This is a place to store 
        # user preferences and caches of product information.
        # 
        # if not userDataDir:
        #     if os.environ.has_key("EUPS_USERDATA"):
        #         userDataDir = os.environ["EUPS_USERDATA"]
        #     else:
        #         userDataDir = os.path.join(os.environ["HOME"], ".eups")
        # if not os.path.exists(userDataDir):
        #     os.makedirs(userDataDir)
        if not os.path.isdir(userDataDir):
            raise RuntimeError("User data directory not found (as a directory): " +
                               userDataDir)
        self.userDataDir = userDataDir

        #
        # Get product information:  
        #   * read the cached version of product info
        #
        self.versions = {}
        neededFlavors = Flavor().getFallbackFlavors(self.flavor, True)
        for p in self.path:

            # the product cache.  If cache is non-existent or out of date,
            # the product info will be refreshed from the database
            cacheDir = p
            dbpath = self.getUpsDB(p)
            if not utils.isDbWritable(p):
                # use a user-writable alternate location for the cache
                cacheDir = self._makeUserCacheDir(p)
            self.versions[p] = ProductStack.fromCache(dbpath, neededFlavors, 
                                                      self.userDataDir, cacheDir,
                                                      updateCache=True, autosave=False)

        # 
        # load up the recognized tags.  
        # 
        self.tags = Tags()
        tags.loadFromEupsPath(self.path)
        tags.loadUserTags(userDataDir)

        #
        # Find locally-setup products in the environment
        #
        self.localVersions = {}

        q = Quiet(self)
        for product in self.getSetupProducts():
            try:
                if re.search(r"^LOCAL:", product.version):
                    self.localVersions[product.name] = os.environ[product.envarDirName()]
            except TypeError:
                pass

    def setPreferredTags(self, tags):
        """
        set a list of tags to prefer when selecting products.  The 
        list order indicates the order of preference with the most 
        preferred tag being first.
        @param tags   the tags as a list or a space-delimited string
        """
        if isinstance(tags, str):
            tags = tags.split()
        if not isinstance(tags, list):
            raise TypeError("Eups.setPreferredTags(): arg not a list")
        tags = filter(self.tags.isRecognized, tags)
        if len(tags) > 0:
            self.preferredTags = tags

    def clearLocks(self):
        """Clear all lock files"""
        for p in self.path + [self.userDataDir]:
            locks = filter(lambda f: f.endswith(".lock"), os.listdir(p))
            for lockfile in locks:
                lockfile = os.path.join(p,lock)
                if self.verbose:
                    print "Removing", lockfile
                try:
                    os.remove(lockfile)
                except Exception, e:
                    print >> sys.stderr, ("Error deleting %s: %s" % (lockfile, e))

    def findSetupVersion(self, productName, environ=None):
        """Find setup version of a product, returning the version, eupsPathDir, productDir, None (for tablefile), and flavor
        If environ is specified search it for environment variables; otherwise look in os.environ
        """

        if not environ:
            environ = os.environ

        versionName, eupsPathDir, productDir, tablefile, flavor = "setup", None, None, None, None
        try:
            args = environ[self._envarSetupName(productName)].split()
        except KeyError:
            return None, eupsPathDir, productDir, tablefile, flavor

        try:
            sproductName = args.pop(0)
        except IndexError:          # Oh dear;  $SETUP_productName must be malformed
            return None, eupsPathDir, productDir, tablefile, flavor
            
        if sproductName != productName:
            if self.verbose > 1:
                print >> sys.stderr, \
                      "Warning: product name %s != %s (probable mix of old and new eups)" %(productName, sproductName)

        if productName == "eups" and not args: # you can get here if you initialised eups by sourcing setups.c?sh
            args = ["LOCAL:%s" % environ["EUPS_DIR"], "-Z", "(none)"]

        if len(args) > 0 and args[0] != "-f":
            versionName = args.pop(0)

        if len(args) > 1 and args[0] == "-f":
            args.pop(0);  flavor = args.pop(0)

        if len(args) > 1 and args[0] == "-Z":
            args.pop(0);  eupsPathDir = args.pop(0)

        assert not args

        if self.tags.isRecognized(versionName):
            dbpath = self.getUpsDB(eupsPathDir)
            vers = Database(dbpath).getTaggedVersion(productName, version, flavor)
            if vers is not None:
                versionName = vers

        try:
            productDir = environ[self._envarDirName()]
        except KeyError:
            pass
            
        return versionName, eupsPathDir, productDir, tablefile, flavor

    def _envarSetupName(self, productName):
        # Return the name of the product's how-I-was-setup environment variable
        name = "SETUP_" + productName

        if os.environ.has_key(name):
            return name                 # exact match

        envNames = filter(lambda k: re.search(r"^%s$" % name, k, re.IGNORECASE), os.environ.keys())
        if envNames:
            return envNames[0]
        else:
            return name.upper()

    def _envarDirName(self, productName):
        # Return the name of the product directory's environment variable
        return self.name.upper() + "_DIR"


    def findProduct(self, name, version=None, eupsPathDirs=None, flavor=None,
                    noCache=False):
        """
        return a product matching the given constraints.  By default, the 
        cache will be searched when available; otherwise, the product 
        database will be searched.  Return None if a match was not found.
        @param name          the name of the desired product
        @param version       the desired version.  This can in one of the 
                                following forms:
                                 *  an explicit version 
                                 *  a version expression (e.g. ">=3.3")
                                 *  a string tag name
                                 *  a Tag instance 
                                 *  null, in which case, the (most) preferred 
                                      version will be returned.
        @param eupsPathDirs  the EUPS path directories to search.  (Each should 
                                have a ups_db sub-directory.)  If None (def.),
                                configured EUPS_PATH directories will be 
                                searched.
        @param flavor        the desired flavor.  If None (default), the 
                                default flavor will be searched for.
        @param noCache       if true, the software inventory cache should not be 
                                used to find products; otherwise, it will be used
                                to the extent it is available.  
        """
        if not version:
            return self.findPreferredProduct(name, eupsPathDirs, flavor)

        if not flavor:
            flavor = self.flavor
        if eupsPathDirs is None:
            eupsPathDirs = self.path

        if isinstance(version, str):
            if version == "setup":
                return findSetupProduct(name)

            if self.isLegalRelativeVersion(version):  # raises exception if bad syntax used
                return _findPreferredProductByExpr(name, version, eupsPathDirs, flavor, noCache)

            if self.tags.isRecognized(version):
                version = self.tags.getTag(version)

        if isinstance(version, Tag):
            # search for a tagged version
            return self._findTaggedProduct(name, version, eupsPathDirs, flavor, noCache)

        # search path for an explicit version 
        for root in eupsPathDirs:
            if noCache or not self.versions.has_key(root) or not self.versions[root]:
                # go directly to the EUPS database
                dbpath = self.getUpsDB(root)
                if not os.path.exists(dbpath):
                    if self.verbose:
                        print >> sys.stderr, "Skipping missing EUPS stack:", dbpath
                    continue

                try:
                    product = Database(dbpath).findProduct(name, version, flavor)
                except ProductNotFound:
                    product = None
    
                if product:
                    return product

            else:
                # consult the cache
                try:
                    return self.versions[root].getProduct(name, version, flavor)
                except ProductNotFound:
                    pass

        return None

    def _findTaggedProduct(self, name, tag, eupsPathDirs, flavor, noCache=False):
        # find the first product assigned a given tag.

        if tag.name == "newest":
            return self._findNewestProduct(name, eupsPathDirs, flavor)

        for root in eupsPathDirs:
            if noCache or not self.versions.has_key(root) or not self.versions[root]:
                # go directly to the EUPS database
                dbpath = self.getUpsDB(root)
                if not os.path.exists(dbpath):
                    if self.verbose:
                        print >> sys.stderr, "Skipping missing EUPS stack:", dbpath
                    continue

                db = Database(dbpath)
                try:
                    version = db.getTaggedVersion(tag.name, name, flavor)
                    if version is not None:
                        return db.findProduct(name, version, flavor)
                except ProductNotFound:
                    # product by this name not found in this database
                    continue

            else:
                # consult the cache
                try: 
                    return self.versions[root].getTaggedProduct(name, tag.name, flavor)
                except ProductNotFound:
                    pass

        return None

    def _findNewestProduct(self, name, eupsPathDirs, flavor, minver=None, 
                           noCache=False):
        # find the newest version of a product.  If minver is not None, 
        # the product must have a version matching this or newer.  
        out = None

        for root in eupsPathDirs:
            if noCache or not self.versions.has_key(root) or not self.versions[root]:
                # go directly to the EUPS database
                dbpath = self.getUpsDB(root)
                if not os.path.exists(dbpath):
                    if self.verbose:
                        print >> sys.stderr, "Skipping missing EUPS stack:", dbpath
                    continue

                products = Database(dbpath).findProducts(name, flavor)
                latest = self._selectPreferredProduct(products, [ Tag("newest") ])
                if latest is None:
                    continue

                # is newest version in this stack newer than minimum version?
                if minver and _version_cmp(latest.version, minver) < 0:
                    continue

                if out == None or _version_cmp(latest.version, out.version) > 0:
                    # newest one in this stack is newest one seen
                    out = latest

            else:
                # consult the cache
                try: 
                    vers = self.versions[root].getVersions(name, flavor)
                    vers.sort(_version_cmp)
                    if len(products) == 0:
                        continue

                    # is newest version in this stack newer than minimum version?
                    if minver and _version_cmp(vers[-1], minver) < 0:
                        continue

                    if out == None or _version_cmp(vers[-1], out.version) > 0:
                        # newest one in this stack is newest one seen
                        out = self.versions[root].getProduct(name, vers[-1], flavor)

                except ProductNotFound:
                    continue

        return out

    def _findPreferredProductByExpr(self, name, expr, eupsPathDirs, flavor):
        return _selectPreferredProduct(self._findProductsByExpr(name, expr, 
                                                                eupsPathDirs, flavor))

    def _findProductsByExpr(self, name, expr, eupsPathDirs, flavor):
        # find the products that satisfy the given expression
        out = []
        outver = []
        for root in eupsPathDirs:
            if noCache or not self.versions.has_key(root) or not self.versions[root]:
                # go directly to the EUPS database
                dbpath = self.getUpsDB(root)
                if not os.path.exists(dbpath):
                    if self.verbose:
                        print >> sys.stderr, "Skipping missing EUPS stack:", dbpath
                    continue

                products = Database(dbpath).findProducts(name, flavor)
                if len(products) == 0: 
                    continue

                products = filter(lambda z: self.version_match(z.version, expr), products)
                for prod in products:
                    if prod.version not in outver:
                        out.append(prod)

            else:
                # consult the cache
                try: 
                    vers = self.versions[root].getVersions(name, flavor)
                    vers = filter(lambda z: self.version_match(z, expr), vers)
                    if len(products) == 0:
                        continue
                    for ver in vers:
                        if ver not in outver:
                            out.append(self.versions[root].getProduct(name, ver, flavor))
                
                except ProductNotFound:
                    continue

        return out

    def findPreferredProduct(self, name, eupsPathDirs, flavor, noCache):
        """
        Find the version of a product that is most preferred or None,
        if no preferred version exists.  

        @param name          the name of the desired product
        @param eupsPathDirs  the EUPS path directories to search.  (Each should 
                                have a ups_db sub-directory.)  If None (def.),
                                configured EUPS_PATH directories will be 
                                searched.
        @param flavor        the desired flavor.  If None (default), the 
                                default flavor will be searched for.
        @param noCache       if true, the software inventory cache should not be 
                                used to find products; otherwise, it will be used
                                to the extent it is available.  
        """
        if not flavor:
            flavor = self.flavor
        if eupsPathDirs is None:
            eupsPathDirs = self.path

        # find all versions of product
        prods = []
        for root in eupsPathDirs:
            if noCache or not self.versions.has_key(root) or not self.versions[root]:
                # go directly to the EUPS database
                dbpath = self.getUpsDB(root)
                if not os.path.exists(dbpath):
                    if self.verbose:
                        print >> sys.stderr, "Skipping missing EUPS stack:", dbpath
                    continue

                prods.extend(Database(dbpath).findProducts(name, flavor=flavor))

            else:
                # consult the cache
                prods.extend(map(lambda v: self.versions[root].getProduct(name,v,flavor), 
                                 self.versions[root].getVersions()))

        return self._selectPreferredProduct(prods, self.perferredTags)

    def _selectPreferredProduct(self, products, preferredTags=None):
        # return the product in a list that is most preferred.
        # None is returned if no products are so tagged.
        # The special "newest" tag will select the product with the latest 
        # version.  
        if not products:
            return None
        if preferredTags is None:
            preferredTags = self.preferredTags

        for tag in preferredTags:
            if tag.name == "newest":
                # find the latest version; first order the versions
                vers = map(lambda p: p.version, products)
                vers.sort(_version_cmp)

                # select the product with the latest version
                if len(vers) > 0:
                    return filter(lambda p: p.version == vers[-1], products)[0]
            else:
                tagged = filter(lambda p: p.isTagged(tag), products)
                if len(tagged) > 0:
                    return tagged[0]
                
        return None


                

        

    def findPreferredProduct(self, name, eupsPathDirs=None, flavor=None, 
                             versions=None):
        """
        return the most preferred version of a product.  The versions parameter
        gives a list of versions to look for in preferred order; the first one
        found will be returned.  Each version will be search for in all of the 
        directories given in eupsPathDirs.
        @param name           the name of the desired product
        @param versions       a list of preferred versions.  Each item
                                may be an explict version, a tag name, or 
                                Tag instance.  The first version found will 
                                be returned.
        """
        if not flavor:
            flavor = self.flavor
        if eupsPathDirs is None:
            eupsPathDirs = self.path

        if versions is None:
            versions = self.preferredTags

        found = None
        for vers in versions:
            found = self.findProduct(name, vers, eupsPathDirs, flavor)
            if found:
                return found

    def getUpsDB(self, eupsPathDir):
        """Return the ups database directory given a directory from self.path"""
        return os.path.join(eupsPathDir, self.ups_db)
    
    def getSetupProducts(self, requestedProductName=None):
        """Return a list of all Products that are currently setup (or just the specified product)"""

        re_setup = re.compile(r"^SETUP_(\w+)$")

        productList = []

        for key in filter(lambda k: re.search(re_setup, k), os.environ.keys()):
            try:
                productName = os.environ[key].split()[0]
            except IndexError:          # Oh dear;  $SETUP_productName must be malformed
                continue

            if requestedProductName and productName != requestedProductName:
                continue

            try:
                product = self.findSetupProduct(requestedProductName)
                if not product and not self.quiet:
                    print >> sys.stderr, "Product %s is not setup" % requestedProductName
                continue

            except RuntimeError, e:
                if not self.quiet:
                    print >> sys.stderr, e
                continue

            productList += [product]

        return productList

    def findSetupProduct(self, productName):
        """
        return a Product instance for a currently setup product.
        """
        versionName, eupsPathDir, productDir, tablefile, flavor = \
            self.findSetupVersion(productName)
        if versionName is None:
            return None
        return Product(productName, versionName, flavor, productDir,
                       tablefile, db=self.getUpsDB(eupsPathDir))
        
    def setEnv(self, key, val, interpolateEnv=False):
        """Set an environmental variable"""
            
        if interpolateEnv:              # replace ${ENV} by its value if known
            val = re.sub(r"(\${([^}]*)})", lambda x : os.environ.get(x.group(2), x.group(1)), val)

        if val == None:
            val = ""
        os.environ[key] = val

    def unsetEnv(self, key):
        """Unset an environmental variable"""

        if os.environ.has_key(key):
            del os.environ[key]

    def setAlias(self, key, val):
        """Set an alias.  The value is in sh syntax --- we'll mangle it for csh later"""

        self.aliases[key] = val

    def unsetAlias(self, key):
        """Unset an alias"""

        if self.aliases.has_key(key):
            del self.aliases[key]
        self.oldAliases[key] = None # so it'll be deleted if no new alias is defined

    def getProduct(self, productName, versionName=None, eupsPathDirs=None, noCache=False):
        """
        select the most preferred product with a given name.  This function is 
        equivalent to 
           findProduct(productName, versionName, eupsPathDirs, flavor=None, 
                       noCache=noCache)
        except that it throws a ProductNotFound exception if it is not found.

        @param name          the name of the desired product
        @param version       the desired version.  This can in one of the 
                                following forms:
                                 *  an explicit version 
                                 *  a version expression (e.g. ">=3.3")
                                 *  a string tag name
                                 *  a Tag instance 
                                 *  null, in which case, the (most) preferred 
                                      version will be returned.
        @param eupsPathDirs  the EUPS path directories to search.  (Each should 
                                have a ups_db sub-directory.)  If None (def.),
                                configured EUPS_PATH directories will be 
                                searched.
        @param noCache       if true, the software inventory cache should not be 
                                used to find products; otherwise, it will be used
                                to the extent it is available.  
        """
        out = self.findFlavor(productName, versionName, eupsPathDirs, noCache=noCache)
        if out is None:
            raise ProductNotFound(productName, versionName, self.flavor)

    def isSetup(self, product, versionName=None, eupsPathDir=None):
        """
        return true if product is setup.

        For backward compatibility, the product parameter can be a Product instance,
        inwhich case, the other parameters are ignored.
        """
        if isinstance(product, Product):
            if product.version is not None:
                versionName = product.version
            if product.db is not None:
                eupsPathDir = product.stackRoot()
            product = product.name

        if not os.environ.has_key(self._envarSetupName(product)):
            return False
        elif versionName is None and eupsPathDir is not None:
            return True

        prod = self.findSetupProduct(product)
        if eupsPathDir is not None and eupsPathDir != prod.stackRoot():
            return False

        return versionName is None or versionName == prod.version

    def unsetupSetupProduct(self, product):
        """ 
        if the given product is setup, unset it up.  
        """
        prod = self.findSetupProduct(product.name)
        if prod is not None:
            try:
                self.setup(prod, fwd=False)
            except RuntimeError, e:
                print >> sys.stderr, \
                    "Unable to unsetup %s %s: %s" % (prod.name, prod.version, e)

    # Permitted relational operators
    _relop_re = re.compile(r"<=?|>=?|==")
    _bad_relop_re = re.compile(r"^\s*=\s+\S+")

    def isLegalRelativeVersion(self, versionName):
        if _relop_re.search(versionName):
            return True
        elif _bad_relop_re.match(versionName):
            raise RuntimeError("Bad expr syntax: %s; did you mean '=='?" % versionName)
        else:
            return False

    def _version_cmp(self, v1, v2):
        """Compare two version strings

    The strings are split on [._] and each component is compared, numerically
    or as strings as the case may be.  If the first component begins with a non-numerical
    string, the other must start the same way to be declared a match.

    If one version is a substring of the other, the longer is taken to be the greater

    If the version string includes a '-' (say VV-EE) the version will be fully sorted on VV,
    and then on EE iff the two VV parts are different.  VV sorts to the RIGHT of VV-EE --
    e.g. 1.10.0-rc2 comes to the LEFT of 1.10.0

    Additionally, you can specify another modifier +FF; in this case VV sorts to the LEFT of VV+FF
    e.g. 1.10.0+hack1 sorts to the RIGHT of 1.10.0

    As an alternative appealing to cvs users, you can replace -EE by mEE or +FF by pFF, but in
    this case EE and FF must be integers
    """

        try:
            return versionCallback.apply(v1, v2, version_cmp)
        except ValueError:
            return None
        except Exception, e:
            print >> sys.stderr, "Detected error running versionCallback: %s" % e
            return None

    def version_match(self, vname, expr):
        """Return vname if it matches the logical expression expr"""

        expr0 = expr
        expr = filter(lambda x: not re.search(r"^\s*$", x), re.split(r"\s*(%s|\|\||\s)\s*" % Eups._relop_re, expr))

        oring = True;                       # We are ||ing primitives
        i = -1
        while i < len(expr) - 1:
            i += 1

            if re.search(Eups._relop_re, expr[i]):
                op = expr[i]; i += 1
                v = expr[i]
            elif re.search(r"^[-+.:/\w]+$", expr[i]):
                op = "=="
                v = expr[i]
            elif expr[i] == "||" or expr[i] == "or":
                oring = True;                     # fine; that is what we expected to see
                continue
            else:
                print >> sys.stderr, "Unexpected operator %s in \"%s\"" % (expr[i], expr0)
                break

            if oring:                # Fine;  we have a primitive to OR in
                if self.version_match_prim(op, vname, v):
                    return vname

                oring = False
            else:
                print >> sys.stderr, "Expected logical operator || in \"%s\" at %s" % (expr0, v)

        return None

    def version_match_prim(self, op, v1, v2):
        """
    Compare two version strings, using the specified operator (< <= == >= >), returning
    true if the condition is satisfied

    Uses _version_cmp to define sort order """

        cmp = self._version_cmp(v1, v2)

        if cmp is None:                 # no sort order is defined
            return False

        if op == "<":
            return cmp <  0
        elif (op == "<="):
            return cmp <= 0
        elif (op == "=="):
            return cmp == 0
        elif (op == ">"):
            return cmp >  0
        elif (op == ">="):
            return cmp >= 0
        else:
            print >> sys.stderr, "Unknown operator %s used with %s, %s--- complain to RHL", (op, v1, v2)

    #
    # Here is the externally visible API
    #
    def setup(self, productName, versionName=None, fwd=True, recursionDepth=0,
              setupToplevel=True, noRecursion=False, setupType=None):
        """The workhorse for setup.  Return (success?, version) and modify self.{environ,aliases} as needed;
        eups.setup() generates the commands that we need to issue to propagate these changes to your shell"""
        #
        # Look for product directory
        #
        setupFlavor = self.flavor            # we may end up using e.g. "Generic"

        product = None
        if isinstance(productName, Product): # it's already a full Product
            product = productName; productName = product.name
        elif not fwd:
            product = self.findSetupProduct(productName)
            if productList:
                product = productList[0]
            else:
                msg = "I can't unsetup %s as it isn't setup" % productName
                if self.verbose > 1 and not self.quiet:
                    print >> sys.stderr, msg

                if not self.force:
                    return False, versionName, msg
                #
                # Fake enough to be able to unset the environment variables
                #
                product = Product(productName)
                product.table = "none"

            if versionName and self.tags.isRecognized(versionName):
                # resolve a tag to a version
                p = self._findTaggedProduct(product, versionName)
                if p and p.version:
                    versionName = p.version

            if not self.version_match(product.version, versionName):
                if not self.quiet:
                    print >> sys.stderr, \
                        "You asked to unsetup %s %s but version %s is currently setup; unsetting up %s" % \
                        (product.name, versionName, product.version, product.version)
        else:
            if self.root and recursionDepth == 0:
                product = self.findProduct(product, versionName, self.root)
            else:
                product = self.findProduct(productName, versionName)
                if not product:
                    if False and self.verbose:
                        print >> sys.stderr, e

                    #
                    # We couldn't find it, but maybe it's already setup locally? That'd be OK
                    #
                    if self.keep and self.alreadySetupProducts.has_key(productName):
                        product = self.alreadySetupProducts[productName]
                    else:
                        #
                        # It's not there.  Try a set of other flavors that might fit the bill
                        #
                        for fallbackFlavor in Flavor().getFallbackFlavors(self.flavor):
                            if flavor == fallbackFlavor:
                                continue

                            product = self.findProduct(productName, versionName, flavor=fallbackFlavor)

                            if product:        
                                setupFlavor = fallbackFlavor
                                if self.verbose > 2:
                                    print >> sys.stderr, "Using flavor %s for %s %s" % \
                                        (setupFlavor, productName, versionName)
                                break

                        if not product:
                            return False, versionName, ProductNotFound(productName, versionName)

        if setupType and not self.tags.isRecognized(setupType):
            raise RuntimeError, ("Unknown type %s; expected one of \"%s\"" % \
                                 (setupType, "\" \"".join(self.tags.getTagNames())))


        #
        # We have all that we need to know about the product to proceed
        #
        # If we're the toplevel, get a list of all products that are already setup
        #
        if recursionDepth == 0:
            if fwd:
                q = Quiet(self)
                self.alreadySetupProducts = {}
                for p in self.getSetupProducts():
                    self.alreadySetupProducts[p.name] = p
                del q

        table = product.table
        if not isinstance(table, Table):
            # product.table should be a path string
            table = Table(table)

        try:
            actions = table.actions(self.flavor, setupType=setupType)
        except ProductNotFound, e:
            print >> sys.stderr, str(e)
            return False, product.version, e
        except RuntimeError, e:
            # is this needed?
            print >> sys.stderr, "product %s %s: %s" % (product.name, product.version, e)
            return False, product.version, e

        #
        # Ready to go
        #
        # self._msgs["setup"] is used to suppress multiple messages about setting up the same product
        if recursionDepth == 0:
            self._msgs["setup"] = {}

        indent = "| " * (recursionDepth/2)
        if recursionDepth%2 == 1:
            indent += "|"

        setup_msgs = self._msgs["setup"]
        if fwd and self.verbose and recursionDepth >= 0:
            key = "%s:%s:%s" % (product.name, self.flavor, product.version)
            if self.verbose > 1 or not setup_msgs.has_key(key):
                print >> sys.stderr, "Setting up: %-30s  Flavor: %-10s Version: %s" % \
                      (indent + product.name, setupFlavor, product.version)
                setup_msgs[key] = 1

        if fwd and setupToplevel:
            #
            # Are we already setup?
            #
            try:
                sprod = self.findSetupProduct(product.name)
            except RuntimeError, e:
                sversionName = None

            if product.version and sprod.version:
                if product.version == sprod.version or productDir == sprod.dir: # already setup
                    if recursionDepth == 0: # top level should be resetup if that's what they asked for
                        pass
                    elif self.force:   # force means do it!; so do it.
                        pass
                    else:
                        if self.verbose > 1:
                            print >> sys.stderr, "            %s %s is already setup; skipping" % \
                                  (len(indent)*" " + product.name, product.version)
                            
                        return True, product.version, None
                else:
                    if recursionDepth > 0: # top level shouldn't whine
                        pversionName = product.version

                        if self.keep:
                            verb = "requesting"
                        else:
                            verb = "setting up"

                        msg = "%s %s is setup, and you are now %s %s" % \
                              (product.name, sprod.version, verb, pversionName)

                        if self.quiet <= 0 and self.verbose > 0 and not (self.keep and setup_msgs.has_key(msg)):
                            print >> sys.stderr, "            %s%s" % (recursionDepth*" ", msg)
                        setup_msgs[msg] = 1

            if recursionDepth > 0 and self.keep and product.name in self.alreadySetupProducts.keys():
                keptProduct = self.alreadySetupProducts[product.name]

                resetup = True          # do I need to re-setup this product?
                if self.isSetup(keptProduct):
                    resetup = False
                    
                if self._version_cmp(product.version, keptProduct.version) > 0:
                    keptProduct = product                     
                    self.alreadySetupProducts[product.name] = product # keep this one instead
                    resetup = True

                if resetup:
                    #
                    # We need to resetup the product, but be careful. We can't just call
                    # setup recursively as that'll just blow the call stack; but we do
                    # want keep to be active for dependent products.  Hence the two
                    # calls to setup
                    #
                    self.setup(keptProduct, recursionDepth=-9999, noRecursion=True)
                    self.setup(keptProduct, recursionDepth=recursionDepth, setupToplevel=False)

                if keptProduct.version != product.version and self.keep and \
                       ((self.quiet <= 0 and self.verbose > 0) or self.verbose > 2):
                    msg = "%s %s is already setup; keeping" % \
                          (keptProduct.name, keptProduct.version)

                    if not setup_msgs.has_key(msg):
                        if not self.verbose:
                            print >> sys.stderr, msg
                        else:
                            print >> sys.stderr, "            %s" % (len(indent)*" " + msg)
                        setup_msgs[msg] = 1

                return True, keptProduct.version, None

            q = Quiet(self)
            self.unsetupSetupProduct(product)
            del q

            self.setEnv(product.envarDirName(), product.dir)
            self.setEnv(product.envarSetupName(),
                        "%s %s -f %s -Z %s" % (product.name, product.version, setupFlavor, product.db))
            #
            # Remember that we've set this up in case we want to keep it later
            #
            if not self.alreadySetupProducts.has_key(product.name):
                self.alreadySetupProducts[product.name] = product
        elif fwd:
            assert not setupToplevel
        else:
            if product.dir in self.localVersions.keys():
                del self.localVersions[product.dir]

            self.unsetEnv(product.envarDirName())
            self.unsetEnv(product.envarSetupName())
        #
        # Process table file
        #
        for a in actions:
            a.execute(self, recursionDepth + 1, fwd, noRecursion=noRecursion)

        if recursionDepth == 0:            # we can cleanup
            if fwd:
                del self.alreadySetupProducts
                del self._msgs["setup"]

        return True, product.version, None

    def unsetup(self, productName, versionName=None):
        """Unsetup a product"""

        return self.setup(productName, versionName, fwd=False)

    def assignTag(self, tag, productName, versionName, eupsPathDir=None):
        """
        assign the given tag to a product.  The product that it will be
        assigned to will be the first product found in the EUPS_PATH
        with the given name and version.  If the product is not found
        a ProductNotFound exception is raised.  If the tag is not 
        supported, a TagNotRecognized exception will be raised
        @param tag           the tag to assign as tag name or Tag instance 
        @param productName   the name of the product to tag
        @param versionName   the version of the product
        """
        # convert tag name to a Tag instance; may raise TagNotRecognized
        tag = self.tags.getTag(tag)

        product = self.getProduct(productName, versionName, eupsPathDir)
        root = product.stackRoot()

        if tag.isGlobal() and not utils.isDbWritable(product.db):
            raise RuntimeError(
                "You don't have permission to assign a global tag %s in %s" %
                (str(tag), product.db))

        # update the database if tag is global
        if tag.isGlobal():
            Database(product.db).assignTag(str(tag), productName, versionName, self.flavor)

        # update the cache
        if self.versions.has_key(root) and self.versions[root]:
            self.versions[root].assignTag(str(tag), productName, versionName, self.flavor)
            try:
                self.versions[root].save(self.flavor)
            except RuntimeError, e:
                if self.quiet < 1:
                    print >> sys.stderr, "Warning: " + str(e)

    def unassignTag(self, tag, productName, versionName=None, eupsPathDir=None):
        """
        unassign the given tag on a product.    
        @param tag           the tag to assign as tag name or Tag instance 
        @param productName   the name of the product to tag
        @param versionName   the version of the product.  If None, choose the
                                 version that currently has the tag.
        @param eupsPathDir   the EUPS stack to find the product in.  If None,
                                 the first product in the stack with that tag
                                 will be chosen.
        """
        # convert tag name to a Tag instance; may raise TagNotRecognized
        tag = self.tags.getTag(tag)

        if versionName or not eupsPathDir or isinstance(eupsPathDir, list):
            # find the appropriate product
            prod = self.findProduct(productName, versionName, eupsPathDir, self.flavor)
            if prod is None:
                raise ProductNotFound(productName, versionName, self.flavor)
            if versionName and versionName != prod.version and self.quiet > 0:
                msg = "Tag %s not assigned to %s %s" % (productName, versionName)
                if eupsPathDir:
                    msg += " in " + str(eupsPathDir)
                print >> sys.stderr, msg
                return
            dbpath = prod.db
        else:
            dbpath = os.join(eupsPathDir, self.ups_db)

        if tag.isGlobal() and not utils.isDbWritable(dbpath):
            raise RuntimeError(
                "You don't have permission to unassign a global tag %s in %s" %
                (str(tag), product.db))

        # update the database
        if tag.isGlobal() and not Database(dbpath).unassignTag(str(tag), productName, self.flavor):
            if self.verbose:
                print >> sys.stderr, "Tag %s not assigned to %s %s" % \
                    (productName, versionName)

        # update the cache
        if self.versions.has_key(root) and self.versions[root]:
            if self.versions[root].unassignTag(str(tag), productName, versionName, self.flavor):
                try:
                    self.versions[root].save(self.flavor)
                except RuntimeError, e:
                    if self.quiet < 1:
                        print >> sys.stderr, "Warning: " + str(e)
            elif self.verbose:
                print >> sys.stderr, "Tag %s not assigned to %s %s" % \
                    (productName, versionName)
                

    def declare(self, productName, versionName, productDir, eupsPathDir=None, tablefile=None, 
                tag=None, declareCurrent=None):
        """ 
        Declare a product.  That is, make this product known to EUPS.  

        If the product is already declared, this method can be used to
        change the declaration.  The most common type of
        "redeclaration" is to only assign a tag.  (Note that this can 
        be accomplished more efficiently with assignTag() as well.)
        Attempts to change other data for a product requires self.force
        to be true. 

        If the product has not installation directory or table file,
        these parameters should be set to "none".  If either are None,
        some attempt is made to surmise what these should be.  If the 
        guessed locations are not found to exist, this method will
        raise an exception.  

        If the tablefile is an open file descriptor, it is assumed that 
        a copy should be made and placed into product's ups directory.
        This directory will be created if it doesn't exist.

        For backward compatibility, the declareCurrent parameter is
        provided but its use is deprecated.  It is ignored unless the
        tag argument is None.  A value of True is equivalent to 
        setting tag="current".  If declareCurrent is None and tag is
        boolean, this method assumes the boolean value is intended for 
        declareCurrent.  
        """
        if re.search(r"[^a-zA-Z_0-9]", productName):
            raise RuntimeError, ("Product names may only include the characters [a-zA-Z_0-9]: saw %s" % productName)

        # this is for backward compatibility
        if isinstance(tag, bool) or (tag is None and declareCurrent):
            tag = "current"
            if not self.quiet:
                print >> sys.stderr, "Eups.declare(): declareCurrent param is deprecated; use tag param."

        if productDir and not productName:
            productName = utils.guessProduct(os.path.join(productDir, "ups"))

        if tag and (not productDir or productDir == "/dev/null" or not tablefile):
            info = self.findProduct(productName, versionName, self.flavor, eupsPathDir)
            if info is not None:
                if not productDir:
                    productDir = info.dir
                if not tablefile:
                    tablefile = info.table # we'll check the other fields later
                if not productDir:
                    productDir = "none"

        if not productDir or productDir == "/dev/null":
            #
            # Look for productDir on self.path
            #
            for eupsProductDir in self.path:
                _productDir = os.path.join(eupsProductDir, self.flavor, productName, versionName)
                if os.path.isdir(_productDir):
                    productDir = _productDir
                    break

        if not productDir:
            raise RuntimeError, \
                  ("Please specify a productDir for %s %s (maybe \"none\")" % (productName, versionName))

        if productDir == "/dev/null":   # Oh dear, we failed to find it
            productDir = "none"
            print >> sys.stderr, "Failed to find productDir for %s %s; assuming \"%s\"" % \
                  (productName, versionName, productDir)

        if utils.isRealFilename(productDir) and not os.path.isdir(productDir):
            raise RuntimeError, \
                  ("Product %s %s's productDir %s is not a directory" % (productName, versionName, productDir))

        if tablefile is None:
            tablefile = "%s.table" % productName

        if utils.isRealFilename(productDir):
            if os.environ.has_key("HOME"):
                productDir = re.sub(r"^~", os.environ["HOME"], productDir)
            if not os.path.isabs(productDir):
                productDir = os.path.join(os.getcwd(), productDir)
            productDir = os.path.normpath(productDir)
            assert productDir

        if not eupsPathDir:             # look for proper home on self.path
            for d in self.path:
                if os.path.commonprefix([productDir, d]) == d and \
                   utils.isDbWritable(self.getUpsDB(d)):
                    eupsPathDir = d
                    break

            if not eupsPathDir:
                eupsPathDir = utils.findWritableDb(self.path)

        elif not utils.isDbWritable(eupsPathDir):
            eupsPathDir = None

        if not eupsPathDir: 
            raise RuntimeError(
                "Unable to find writable stack in EUPS_PATH to declare %s %s" % 
                (productName, versionName))

        ups_dir, tablefileIsFd = "ups", False
        if not utils.isRealFilename(tablefile):
            ups_dir = None
        elif tablefile:
            if isinstance(tablefile, file):
                tablefileIsFd = True
                tfd = tablefile

                tablefile = "%s.table" % versionName

                ups_dir = os.path.join("$UPS_DB",               productName, self.flavor)
                tdir = os.path.join(self.getUpsDB(eupsPathDir), productName, self.flavor)

                if not os.path.isdir(tdir):
                    os.makedirs(tdir)
                ofd = open(os.path.join(tdir, tablefile), "w")
                for line in tfd:
                    print >> ofd, line,
                del ofd
        #
        # Check that tablefile exists
        #
        assert tablefile
        if not tablefileIsFd and utils.isRealFilename(tablefile):
            if utils.isRealFilename(productDir):
                if ups_dir:
                    try:
                        full_tablefile = os.path.join(ups_dir, tablefile)
                    except Exception, e:
                        raise RuntimeError, ("Unable to generate full tablefilename: %s" % e)
                    
                    if not os.path.isfile(full_tablefile) and not os.path.isabs(full_tablefile):
                        full_tablefile = os.path.join(productDir, full_tablefile)

                else:
                    full_tablefile = tablefile
            else:
                full_tablefile = os.path.join(productDir, ups_dir, tablefile)

            if not os.path.isfile(full_tablefile):
                raise RuntimeError, ("I'm unable to declare %s as tablefile %s does not exist" %
                                     (productName, full_tablefile))
        else:
            full_tablefile = None

        #
        # See if we're redeclaring a product and complain if the new declaration conflicts with the old
        #
        dodeclare = True
        prod = self.findProduct(productName, versionName, eupsPathDir)
        if prod is not None and not self.force:
            _version, _eupsPathDir, _productDir, _tablefile = \
                      prod.version, prod.stackDir(), prod.dir, prod.table

            assert _version == versionName
            assert eupsPathDir == _eupsPathDir

            differences = []
            if _productDir and productDir != _productDir:
                differences += ["%s != %s" % (productDir, _productDir)]

            if full_tablefile and _tablefile and tablefile != _tablefile:
                # Different names; see if they're different content too
                diff = ["%s != %s" % (tablefile, _tablefile)] # possible difference
                try:
                    if not filecmp.cmp(full_tablefile, _tablefile):
                        differences += diff
                except OSError:
                    differences += diff

            if differences:
                # we're in a re-declaring situation
                info = ""
                if self.verbose:
                    info = " (%s)" % " ".join(differences)
                raise RuntimeError, ("Redeclaring %s %s%s; specify force to proceed" %
                                     (productName, versionName, info))

            elif _productDir and _tablefile:
                # there's no difference with what's already declared
                dodeclare = False

        #
        # Arguments are checked; we're ready to go
        #
        verbose = self.verbose
        if self.noaction:
            verbose = 2
        if not dodeclare:
            if tag:
                # we just want to update the tag
                if verbose:
                    info = "Assigning %s to %s %s" % (tag, productName, versionName)
                    print >> sys.stderr, info
                if not self.noaction:
                    self.assignTag(tag, productName, versionName)
            return

        # Talk about doing a full declare.  
        if verbose:
            info = "Declaring"
            if verbose > 1:
                if productDir == "/dev/null":
                    info += " \"none\" as"
                else:
                    info += " %s as" % productDir
            info += " %s %s" % (productName, versionName)
            if tag:
                info += " %s" % tag
            info += " in %s" % (eupsPathDir)

            print >> sys.stderr, info
        if self.noaction:  
            return

        # now really declare the product.  This will also update the tags
        dbpath = self.getUpsDB(eupsPathDir)
        product = Product(productName, versionName, self.flavor, productDir, 
                          tablefile, tag, dbpath)

        Database(dbpath).declare(product)
        if self.versions.has_key(eupsPathDir) and self.versions[eupsPathDir]:
            self.versions[eupsPathDir].addProduct(product)
            try:
                self.versions[eupsPathDir].save(self.flavor)
            except RuntimeError, e:
                if self.quiet < 1:
                    print >> sys.stderr, "Warning: " + str(e)
                

    def undeclare(self, productName, versionName=None, eupsPathDir=None, tag=None, 
                  undeclareCurrent=None):
        """
        Undeclare a product.  That is, remove knowledge of this
        product from EUPS.  This method can also be used to just
        remove a tag from a product without fully undeclaring it.

        A tag parameter that is not None indicates that only a 
        tag should be de-assigned.  (Note that this can 
        be accomplished more efficiently with unassignTag() as 
        well.)  In this case, if versionName is None, it will 
        apply to any version of the product.  If eupsPathDir is None,
        this method will attempt to undeclare the first matching 
        product in the default EUPS path.  

        For backward compatibility, the undeclareCurrent parameter is
        provided but its use is deprecated.  It is ignored unless the
        tag argument is None.  A value of True is equivalent to 
        setting tag="current".  If undeclareCurrent is None and tag is
        boolean, this method assumes the boolean value is intended for 
        undeclareCurrent.  
        """
        # this is for backward compatibility
        if isinstance(tag, bool) or (tag is None and undeclareCurrent):
            tag = "current"
            if not self.quiet:
                print >> sys.stderr, "Eups.undeclare(): undeclareCurrent param is deprecated; use tag param."

            return unassignTag(productName, versionName, eupsPathDir)

        product = None
        if not versionName:
            productList = self.findProducts(productName, eupsPathDir=eupsPathDir) 
            if len(productList) == 0:
                raise ProductNotFound(productName, eupsPathDir=eupsPathDir)

            elif len(productList) > 1:
                versionList = map(lambda el: el.version, productList)
                raise RuntimeError, ("Product %s has versions \"%s\"; please choose one and try again" %
                                     (productName, "\" \"".join(versionList)))

            else:
                versionName = productList[0].version
            
        # this raises ProductNotFound if not found
        product = self.getProduct(productName, versionName, eupsPathDir)
        eupsPathDir = os.path.dirname(product.db)

        if not utils.isDbWritable(product.db):
            raise RuntimeError("You do not have permission to undeclare products from %s" % eupsPathDir)
            
        if self.isSetup(product):
            if self.force:
                print >> sys.stderr, "Product %s %s is currently setup; proceeding" % (productName, versionName)
            else:
                raise RuntimeError, \
                      ("Product %s %s is already setup; specify force to proceed" % (productName, versionName))

        if self.verbose or self.noaction:
            print >> sys.stderr, "Removing %s %s from version list for %s" % \
                (product.name, product.version, product.stackRoot())
        if self.noaction:
            return True

        if not Database(dbpath).undeclare(product):
            # this should not happen
            raise ProductNotFound(product.name, product.version, product.flavor, product.db)
            
        if self.versions.has_key(eupsPathDir) and self.versions[eupsPathDir]:
            self.versions[eupsPathDir].removeProduct(product)
            try:
                self.versions[eupsPathDir].save(self.flavor)
            except RuntimeError, e:
                if self.quiet < 1:
                    print >> sys.stderr, "Warning: " + str(e)

        return True

    def listProducts(self, productName=None, productVersion=None,
                     tags=None, current=None, setup=False):
        """
        Return a list of Product objects for products we know about
        with given restrictions. 

        The returned list will be restrict by the name, version,
        and/or tag assignment using the productName, productVersion,
        and tags parameters, respectively.  productName and 
        productVersion can have shell wildcards (like *); in this 
        case, they will be matched in a shell globbing-like way 
        (using fnmatch).  

        current and setup are provided for backward compatibility, but
        are deprecated.  
        """

        productList = []
        #
        # Maybe they wanted Setup or some sort of Current?
        #
        if productVersion == Setup():
            setup = True
        elif isSpecialVersion(productVersion, setup=False):
            current = productVersion

        if current or setup:
            productVersion = None
        #
        # Find all products on path (cached in self.versions, of course)
        #
        for db in self.path:
            for flavor in Flavor().getFallbackFlavors(self.flavor, True):
                if not self.versions.has_key(db) or not self.versions[db].has_key(flavor):
                    continue

                for name in self.versions[db][flavor].keys():
                    if productName and not fnmatch.fnmatchcase(name, productName):
                        continue

                    for version in self.versions[db][flavor][name].keys():
                        if productVersion and not fnmatch.fnmatchcase(version, productVersion):
                            continue

                        product = self.versions[db][flavor][name][version]
                        product.Eups = self     # don't use the cached Eups

                        isCurrent = product.checkCurrent(currentType=current)
                        isSetup = self.isSetup(product)

                        if current and current != isCurrent:
                            continue

                        if setup and not isSetup:
                            continue

                        productList.append(ProductInformation(name,
                                                              version, db, product.dir, isCurrent, isSetup, flavor))
        #
        # Add in LOCAL: setups
        #
        for lproductName in self.localVersions.keys():
            product = self.Product(lproductName, noInit=True)

            if not setup and (productName and productName != lproductName): # always print local setups of productName
                continue

            try:
                product.initFromDirectory(self.localVersions[product.name])
            except RuntimeError, e:
                if not self.quiet:
                    print >> sys.stderr, ("Problem with product %s found in environment: %s" % (lproductName, e))
                continue

            if productName and not fnmatch.fnmatchcase(product.name, productName):
                continue
            if productVersion and not fnmatch.fnmatchcase(product.version, productVersion):
                continue

            thisCurrent = current
            if current:
                isCurrent = product.checkCurrent()
                if current != isCurrent:
                    if productName == lproductName and current != Current():
                        thisCurrent = Current(" ") # they may have setup -r . --tag=XXX
                    else:
                        continue

            productList.append(ProductInformation(product.name,
                                                  product.version, product.db, product.dir, thisCurrent, True, flavor))
        #
        # And sort them for the end user
        #
        def sort_versions(a, b):
            if a.name == b.name:
                return version_cmp(a.version, b.version)
            else:
                return cmp(a.name, b.name)
            
        productList.sort(sort_versions)

        return productList

    def dependencies_from_table(self, tablefile, eupsPathDirs=None, setupType=None):
        """Return self's dependencies as a list of (Product, optional, currentRequested) tuples

        N.b. the dependencies are not calculated recursively"""
        dependencies = []
        if utils.isRealFilename(tablefile):
            for (product, optional, currentRequested) in \
                    Table(tablefile).dependencies(self, eupsPathDirs, setupType=setupType):
                dependencies += [(product, optional, currentRequested)]

        return dependencies

    def remove(self, productName, versionName, recursive, checkRecursive=False, interactive=False, userInfo=None):
        """Undeclare and remove a product.  If recursive is true also remove everything that
        this product depends on; if checkRecursive is True, you won't be able to remove any
        product that's in use elsewhere unless force is also True.

        N.b. The checkRecursive option is quite slow (it has to parse
        every table file on the system).  If you're calling remove
        repeatedly, you can pass in a userInfo object (returned by
        self.uses(None)) to save remove() having to processing those
        table files on every call."""
        #
        # Gather the required information
        #
        if checkRecursive and not userInfo:
            if self.verbose:
                print >> sys.stderr, "Calculating product dependencies recursively..."
            userInfo = self.uses(None)
        else:
            userInfo = None

        topProduct = productName
        topVersion = versionName
        #
        # Figure out what to remove
        #
        productsToRemove = self._remove(productName, versionName, recursive, checkRecursive,
                                        topProduct, topVersion, userInfo)

        productsToRemove = list(set(productsToRemove)) # remove duplicates
        #
        # Actually wreak destruction. Don't do this in _remove as we're relying on the static userInfo
        #
        default_yn = "y"                    # default reply to interactive question
        removedDirs = {}                    # directories that have already gone (useful if more than one product
                                            # shares a directory)
        removedProducts = {}                # products that have been removed (or the user said no)
        for product in productsToRemove:
            dir = product.dir
            if False and not dir:
                raise RuntimeError, \
                      ("Product %s with version %s doesn't seem to exist" % (product.name, product.version))
            #
            # Don't ask about the same product twice
            #
            pid = product.__str__()
            if removedProducts.has_key(pid):
                continue

            removedProducts[pid] = 1

            if interactive:
                yn = default_yn
                while yn != "!":
                    yn = raw_input("Remove %s %s: (ynq!) [%s] " % (product.name, product.version, default_yn))

                    if yn == "":
                        yn = default_yn
                    if yn == "y" or yn == "n" or yn == "!":
                        default_yn = yn
                        break
                    elif yn == "q":
                        return
                    else:
                        print >> sys.stderr, "Please answer y, n, q, or !, not %s" % yn

                if yn == "n":
                    continue

            if not self.undeclare(product.name, product.version, undeclareCurrent=None):
                raise RuntimeError, ("Not removing %s %s" % (product.name, product.version))

            if removedDirs.has_key(dir): # file is already removed
                continue

            if utils.isRealFilename(dir):
                if self.noaction:
                    print "rm -rf %s" % dir
                else:
                    try:
                        shutil.rmtree(dir)
                    except OSError, e:
                        raise RuntimeError, e

            removedDirs[dir] = 1

    def _remove(self, productName, versionName, recursive, checkRecursive, topProduct, topVersion, userInfo):
        """The workhorse for remove"""

        try:
            product = Product(self, productName, versionName)
        except RuntimeError, e:
            raise RuntimeError, ("product %s %s doesn't seem to exist" % (productName, versionName))

        deps = [(product, False, False)]
        if recursive:
            deps += product.dependencies()

        productsToRemove = []
        for product, o, currentRequested in deps:
            if checkRecursive:
                usedBy = filter(lambda el: el[0] != topProduct or el[1] != topVersion,
                                userInfo.users(product.name, product.version))

                if usedBy:
                    tmp = []
                    for user in usedBy:
                        tmp += ["%s %s" % (user[0], user[1])]

                    if len(tmp) == 1:
                        plural = ""
                        tmp = str(tmp[0])
                    else:
                        plural = "s"
                        tmp = "(%s)" % "), (".join(tmp)

                    msg = "%s %s is required by product%s %s" % (product.name, product.version, plural, tmp)

                    if self.force:
                        print >> sys.stderr, "%s; removing anyway" % (msg)
                    else:
                        raise RuntimeError, ("%s; specify force to remove" % (msg))

            if recursive:
                productsToRemove += self._remove(product.name, product.version, (product.name != productName),
                                                 checkRecursive, topProduct=topProduct, topVersion=topVersion,
                                                 userInfo=userInfo)

            productsToRemove += [product]
                
        return productsToRemove

    def uses(self, productName=None, versionName=None, depth=9999):
        """Return a list of all products which depend on the specified product in the form of a list of tuples
        (productName, productVersion, (versionNeeded, optional))

        depth tells you how indirect the setup is (depth==1 => product is setup in table file,
        2 => we set up another product with product in its table file, etc.)

        versionName may be None in which case all versions are returned.  If product is also None,
        a Uses object is returned which may be used to perform further uses searches efficiently
    """
        if not productName and versionName:
            raise RuntimeError, ("You may not specify a version \"%s\" but not a product" % versionName)

        self.exact_version = True

        productList = self.listProducts(None)

        if not productList:
            return []

        useInfo = Uses()

        for pi in productList:          # for every known product
            try:
                q = Quiet(self)
                deps = Product(self, pi.name, pi.version).dependencies() # lookup top-level dependencies
                del q
            except RuntimeError, e:
                print >> sys.stderr, ("%s %s: %s" % (pi.name, pi.version, e))
                continue

            for pd, od, currentRequested in deps:
                if pi.name == pd.name and pi.version == pd.version:
                    continue

                useInfo._remember(pi.name, pi.version, (pd.name, pd.version, od, currentRequested))

        useInfo._invert(depth)
        #
        # OK, we have the information stored away
        #
        if not productName:
            return useInfo

        return useInfo.users(productName, versionName)

    # =-=-=-=-=-=-=-=-=-= DEPRECATED METHODS =-=-=-=-=-=-=-=-=-=-=
    def setCurrentType(self, currentType):
        """Set type of "Current" we want (e.g. current, stable, ...)"""
        if self.quiet <= 0:
            print sys.stderr, \
                "Deprecated function: Eups.setCurrentType(); use setPreferredTags()"
        return setPreferredTag(currentType)

    def getCurrent(self):
        if self.quiet <= 0:
            print sys.stderr, \
                "Deprecated function: Eups.getCurrent(); use getPreferredTags()"
        return self.preferredTags[0]

    def findVersion(self, productName, versionName=None, eupsPathDirs=None, allowNewer=False, flavor=None):
        """
        Find a version of a product.  This function is DEPRECATED; use
        findProduct() instead.  

        If no version is specified, the most preferred tagged version
        is returned.  The return value is: versionName, eupsPathDir,
        productDir, tablefile 

        If allowNewer is true, look for versions that are >= the
        specified version if an exact match fails.
        """
        if not flavor:
            flavor = self.flavor

        prod = self.findVersion(productName, versionName, eupsPathDirs, flavor)

        msg = "Unable to locate product %s %s for flavor %s." 
        if not prod and allowNewer:
            # an explicit version given; try to find a newer one
            if self.quiet <= 0:
                print >> sys.stderr, msg % (productName, versionName, flavor), \
                    ' Trying ">= %s"' % versionName

            if self.tags.isRecognized(versionName):
                versionName = None
            prod = self._findNewestVersion(productName, eupsPathDirs, flavor, 
                                           versionName)
        if not prod:
            raise RuntimeError(msg %s (productName, versionName, flavor))

    def findCurrentVersion(self, productName, path=None, currentType=None, currentTypesToTry=None):
        """
        Find current version of a product, returning eupsPathDir, version, vinfo, currentTag.
        DEPRECATED: use findPreferredProduct()
        """
        if not path:
            path = self.path
        elif isinstance(path, str):
            path = [path]

        preferred = currentTypesToTry
        if preferred is None:
            preferred = []
        if currentType is None:
            preferred.insert(currentType, 0)
        if not preferred:
            preferred = None

        out = self.findPreferredProduct(productName, path, self.flavor, preferred)

        if not out:
            raise RuntimeError, \
                  ("Unable to locate a preferred version of %s for flavor %s" %
                   (productName, self.flavor))
        return out

    def findFullySpecifiedVersion(self, productName, versionName, flavor, eupsPathDir):
        """
        Find a version given full details of where to look
        DEPRECATED: use findProduct()
        """
        try: 
            out = self.findProduct(productName, versionName, eupsPathDir, flavor)
            if not out:
                raise ProductNotFound(productName, versionName, flavor, eupsPathDir)
        except ProductNotFound, e:
            raise RuntimeError(e.getMessage())

    def declareCurrent(self, productName, versionName, eupsPathDir=None, local=False):
        """Declare a product current.
        DEPRECATED: use assignTag()
        """
        if not self.quiet:
            print >> sys.stderr, "Warning: Eups.declareCurrent() DEPRECATED; use assignTag()"

        # this will raise an exception if "current" is not allowed
        tag = self.tags.getTag("current")
        self.assignTag(tag, productName, versionName, eupsPathDir)

    def removeCurrent(self, product, eupsPathDir, currentType=None):
        """Remove the CurrentChain for productName/versionName from the live current chain (of type currentType)
        DEPRECATED: use assignTag()
        """
        if not self.quiet:
            print >> sys.stderr, "Warning: Eups.remvoeCurrent() DEPRECATED; use unassignTag()"

        # this will raise an exception if "current" is not allowed
        if currentType is None:
            currentType = "current"
        tag = self.tags.getTag(currentType)
        self.unassignTag(tag, product, eupsPathDir=eupsPathDir)




_ClassEups = Eups                       # so we can say, "isinstance(Eups, _ClassEups)"

