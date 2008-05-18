#!/usr/bin/env python
# -*- python -*-
#
# Export a product and its dependencies as a package, or install a
# product from a package
#
import atexit
import os, stat
import re, sys
import pdb
import tempfile
import shutil
import urllib, urllib2
import eupsGetopt
if True:
    import eups
    import neups
else:
    import neups as eups

author = "Robert Lupton (rhl@astro.princeton.edu)"
eups_distrib_version = "1.0"

if False:
    import warnings
    warnings.filterwarnings('ignore', "tmpnam ", RuntimeWarning, "", 0) # ignore tmpnam warnings

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

URL, SCP, LOCAL = "URL", "SCP", "LOCAL"

class Distrib(object):
    """A class to encapsulate product distribution"""

    def __init__(self, Eups, packageBase, transport, installFlavor=None, preferFlavor=False,
                 current=False, no_dependencies=False, noeups=False):
        self.Eups = Eups
        
        self.packageBase = packageBase
        self.transport = transport
        if not installFlavor:
            installFlavor = Eups.flavor
        self.installFlavor = installFlavor
        self.preferFlavor = preferFlavor
        self.current = current
        self.no_dependencies = no_dependencies
        self.noeups = noeups

    def find_file(self, filename, packagePath=None):
        """Lookup a filename given a (possibly incomplete) packageBase and filename

        N.B. Modifies the self.packageBase if needs be"""

        locs = self.createLocationList(filename, packagePath)

        subDirs = [""]
        if re.search('\.build$', filename):
            subDirs += ['builds']
        elif re.search('\.manifest$', filename):
            subDirs += ['manifests']
        elif re.search('\.table$', filename):
            subDirs += ['tables']

        if self.Eups.verbose > 1:
            print >> sys.stderr, "Looking for %s in subdirectories [%s] of:" % \
                  (filename, str.join(", ", map(lambda sd: "\"%s\"" % sd, subDirs))), \
                  str.join("", map(lambda str: "\n   %s" % os.path.join(self.packageBase, str), locs))

        if self.transport != LOCAL:
            tfile = None
            for loc in locs:
                for sd in subDirs:
                    try:
                        extendedPackageBase = os.path.join(self.packageBase, loc)
                        (tfile, msg) = file_retrieve(os.path.join(extendedPackageBase, sd, filename), self.transport)
                        self.packageBase = extendedPackageBase
                        if self.Eups.verbose > 0:
                            print >> sys.stderr, "Found %s in %s" % (filename, self.packageBase)
                        break
                    except RuntimeError:
                        pass

            if tfile:
                filename = tfile
            else:
                raise RuntimeError, ("Failed to find and retrieve filename %s from %s" % (filename, self.packageBase))
        else:
            if False and not self.packageBase:
                self.packageBase = "%s/packages/%s" % (db, installFlavor)

            tfile = None
            for loc in locs:
                for sd in subDirs:
                    guess = os.path.join(self.packageBase, loc, sd, filename)
                    if os.path.isfile(guess) or os.path.isdir(guess):
                        tfile = guess
                        break
                if tfile:
                    break

            if tfile is None:
                raise RuntimeError, ("File %s doesn't exist in %s" % (filename, self.packageBase))

            filename = tfile

        if self.Eups.verbose > 1:
            print >> sys.stderr, "Found %s" % (filename)

        return filename

    #-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

    def lookup_current_version(self, productName, from_eups=True):
        """Attempt to lookup a package's current version, as declared using eups_distrib"""

        if from_eups:
            return eups.current(productName, self.Eups.dbz, self.Eups.flavor)
        else:
            try:
                for p in Current().read():
                    (name, version) = p
                    if name == productName:
                        return version
            except:
                pass

        return ""

    #-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

    def createLocationList(self, filename, packagePath):
        """Create a list of places to look"""

        locs = []
        if self.preferFlavor:
            locs.extend(['%s' % self.installFlavor, ''])
        else:
            locs.extend(['', '%s' % self.installFlavor])

        if packagePath is not None:
            if self.preferFlavor:
                if tag is not None:
                    locs.extend(['%s/%s/%s' % (packagePath, self.installFlavor, tag),
                                 '%s/%s' % (packagePath, self.installFlavor),
                                 '%s/%s' % (packagePath, tag),
                                 '%s' % (packagePath)])
                else:
                    locs.extend(['%s/%s' % (packagePath, self.installFlavor),
                                 '%s' % (packagePath)])
            else:
                if tag is not None:
                    locs.extend(['%s/%s' % (packagePath, tag),
                                 '%s/%s/%s' % (packagePath, self.installFlavor, tag),
                                 '%s' % (packagePath),
                                 '%s/%s' % (packagePath, self.installFlavor)])
                else:
                    locs.extend(['%s' % (packagePath),
                                 '%s/%s' % (packagePath, self.installFlavor)])

        return locs

    #-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

    def read_manifest(self, top_product, top_version, manifest):
        """Read a manifest and return the product and version, and products_root, top_version, and a list of products"""

        products_root = self.Eups.path[0]
        flavor_dir = "%s/%s" % (products_root, self.Eups.flavor)    # where to install
        if True or os.path.isdir(flavor_dir): # Always use the flavor_dir as products_root
            products_root = flavor_dir

        if manifest:
            raw_manifest = manifest
        else:
            if not top_version:
                top_version = self.lookup_current_version(top_product, from_eups = False)

                if top_version == "":
                    raise RuntimeError, (("No version of %s is declared current to eups distrib\n" + \
                                          "Please specify a version or a manifest file with -m") % (top_product))

                print >> sys.stderr, "Installing %s of %s" % (top_version, top_product)

            raw_manifest = manifestFile(top_product, top_version)
            manifest = self.find_file(raw_manifest)

            if self.transport == LOCAL:
                mat = re.search(r"^(.*)/([^/]+)$", manifest)
                if mat:
                    pb = mat.groups()[0]
                    if self.packageBase and self.packageBase != os.path.commonprefix([self.packageBase, pb]):
                        print >> sys.stderr, "Warning: manifest file %s has different base from -r %s" % \
                              (manifest, self.packageBase)
                        self.packageBase = pb
        #
        # OK, we've found the manifest (phew)
        #
        if self.Eups.verbose > 0:
            if manifest == raw_manifest:
                print >> sys.stderr, "Manifest is", manifest
            else:
                print >> sys.stderr, "Manifest is", raw_manifest, "(%s)" % manifest
        if self.Eups.verbose > 2:
            try:
                fd = open(manifest, "r")
                print "Manifest file:\n\t", "\t".join(fd.readlines()),
                del fd
            except:
                pass

        try:
            fd = open(manifest, "r")
        except:
            raise IOError, ("Failed to open", manifest)

        line = fd.readline()
        mat = re.search(r"^EUPS distribution manifest for (\S+) \((\S+)\). Version (\S+)\s*$", line)
        if not mat:
            raise RuntimeError, ("First line of file %s is corrupted:\n\t%s" % (manifest, line))
        manifest_product, manifest_product_version, version = mat.groups()

        version = mat.groups()[2]
        if version != eups_distrib_version:
            print >> sys.stderr, "WARNING. Saw version %s; expected %s" % (version, eups_distrib_version)

        products = []
        for line in fd:
            line = line.split("\n")[0]
            if re.search(r"^\s*#", line):
                continue

            try:
                products += [re.findall(r"\S+", line)[0:6]]
            except:
                raise RuntimeError, ("Failed to parse line:", line)

        return manifest_product, manifest_product_version, products_root, top_version, products

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

def system(cmd):
    """Run a command, throwing an exception if a non-zero exit code is returned
    Obeys noaction"""

    if noaction:
        print cmd
    else:
        errno = os.system(cmd)
        if errno != 0:
            raise OSError, ("\n\t".join(("Command:\n" + cmd).split("\n")) + ("\nexited with code %d" % (errno >> 8)))

def issamefile(file1, file2):
    """Are two files identical?"""

    try:
        return os.path.samefile(file1, file2)
    except OSError:
        pass

    return False

def copyfile(file1, file2):
    """Like shutil.copy2, but don't fail copying a file onto itself"""

    if issamefile(file1, file2):
        return

    try:
        os.unlink(file2)
    except OSError:
        pass

    shutil.copy2(file1, file2)

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

def currentFile():
    """Return the name of a current-versions file"""
    return "current.list"
    
def manifestFile(product, version):
    """Return the name of a manifest file"""
    return "%s-%s.manifest" % (product, version)

def get_tabledir(transport):
    tabledir = "%s/tables" % (package_base)
    if transport == LOCAL and not os.path.isdir(tabledir):
        print >> sys.stdout, "Creating %s" % (tabledir)
        os.mkdir(tabledir)

    return tabledir

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
#
# Handle distribution via tarballs
#
def write_tarball(basedir, product_dir, package_base, product, version):
    """Create a tarball """
    
    tarball = "%s-%s.tar.gz" % (product, version)

    if os.access("%s/%s" % (package_base, tarball), os.R_OK) and not force:
        if verbose > 0:
            print >> sys.stderr, "Not recreating", tarball
        return tarball
    
    if verbose > 0:
        print >> sys.stderr, "Writing", tarball

    try:
        system("cd %s && tar -cf - %s | gzip > %s/%s" % (basedir, product_dir, package_base, tarball))
    except Exception, param:
        os.unlink("%s/%s" % (package_base, tarball))
        raise OSError, "Failed to write %s/%s" % (package_base, tarball)

    return tarball

def install_from_tarball(products_root, package_base, tarball):
    """Retrieve and unpack a tarball"""
    
    tfile = "%s/%s" % (package_base, tarball)

    if transport != LOCAL and not noaction:
        (tfile, msg) = file_retrieve(tfile, transport)

    if not noaction and not os.access(tfile, os.R_OK):
        raise IOError, ("Unable to read %s" % (tfile))

    if verbose > 0:
        print >> sys.stderr, "installing %s into %s" % (tarball, products_root)

    try:
        system("cd %s && cat %s | gunzip -c | tar -xf -" % (products_root, tfile))                
    except:
        raise IOError, ("Failed to read %s" % (tfile))

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
#
# Handle distribution via cvs/svn and explicit build files
#
def write_builder(basedir, product_dir, package_base, product, version, buildFilePath):
    """Given a buildfile (which contains information about its' CVS/SVN root,
    write a small file to the manifest directory allowing us to bootstrap the
    build.  The build file is looked for in buildFilePath, a : separated set of
    directories ("" -> the installed product's ups directory)"""

    builder = "%s-%s.build" % (product, version)
    buildFile = find_file_on_path("%s.build" % product, buildFilePath, os.path.join(basedir, product_dir, "ups"))

    if not buildFile:
        print >> sys.stderr, "I can't find a build file %s.build anywhere on \"%s\"" % (product, buildFilePath)
        if os.path.exists(os.path.join("ups", "%s.build" % product)):
            print >> sys.stderr, "N.b. found %s.build in ./ups; consider adding ./ups to --build path" % (product)
        if force:
            return None

        raise RuntimeError, "I'm giving up. Use --force if you want to proceed with a partial distribution"

    builderDir = os.path.join(package_base, "builds")
    if not os.path.isdir(builderDir):
        try:
            os.mkdir(builderDir)
        except:
            raise RuntimeError, ("Failed to create %s" % (builderDir))
    
    full_builder = os.path.join(builderDir, builder)
    if os.access(full_builder, os.R_OK) and not force:
        if verbose > 0:
            print >> sys.stderr, "Not recreating", full_builder
        return "build:" + builder
    
    if verbose > 0:
        print >> sys.stderr, "Writing", full_builder

    try:
        if not noaction:
            if False:
                copyfile(buildFile, full_builder)
            else:
                try:
                    ifd = open(buildFile)
                except IOError, e:
                    raise RuntimeError, ("Failed to open file \"%s\" for read" % buildFile)
                try:
                    ofd = open(full_builder, "w")
                except IOError, e:
                    raise RuntimeError, ("Failed to open file \"%s\" for write" % full_builder)

                try:
                    neups.expandBuildFile(ofd, ifd, product, version, verbose)
                except RuntimeError, e:
                    raise RuntimeError, ("Failed to expand build file \"%s\": %s" % (full_builder, e))

                del ifd; del ofd
    except IOError, param:
        try:
            os.unlink(full_builder)
        except OSError:
            pass                        # probably didn't exist
        raise RuntimeError ("Failed to write %s: %s" % (full_builder, param))

    return "build:" + builder

def install_from_builder(Distrib, setups, products_root, package_base, builder, build_dir=None):
    """Setups is a list of setup commands needed to build this product"""
    
    tfile = Distrib.find_file(builder)

    if False:
        if not noaction and not os.access(tfile, os.R_OK):
            raise RuntimeError, ("Unable to read %s" % (tfile))

    if not build_dir:
        build_dir = os.path.join(products_root, "EupsBuildDir")

    if not os.path.isdir(build_dir):
        if not noaction:
            try:
                os.makedirs(build_dir)
            except OSError, e:
                print >> sys.stderr, "Failed to create %s: %s" % (build_dir, e)
    
    if verbose > 0:
        print >> sys.stderr, "Building %s in %s" % (builder, build_dir)
    #
    # Does this build file look OK?  In particular, does it contain a valid
    # CVS/SVN location or curl/wget command?
    #
    (cvsroot, svnroot, url, other) = get_root_from_buildfile(tfile)
    if not (cvsroot or svnroot or url or other):
        if force:
            action = "continuing"
        else:
            action = "aborting"
        msg = "Warning: unable to find a {cvs,svn}root or wget/curl command in %s; %s" % (tfile, action)
        if force:
            print >> sys.stderr, msg
        else:
            raise RuntimeError, msg
    #
    # Prepare to actually do some work
    #
    cmd = ["cd %s" % (build_dir)]
    cmd += setups

    if verbose > 2:
        cmd += ["set -vx"]
    #
    # Rewrite build file to replace any setup commands by "setup -j" as
    # we're not necessarily declaring products current, so we're setting
    # things up explicitly and a straight setup in the build file file
    # undo our hard work
    #
    try:
        fd = open(tfile)
    except IOError, e:
        raise RuntimeError, ("Failed to open %s: %s" % (tfile, e))

    for line in fd:
        line = re.sub(r"\n$", "", line) # strip newline

        if re.search("^#!/bin/(ba|k)?sh", line):      # a #!/bin/sh line; not needed
            continue

        line =  re.sub(r"^(\s*)#(.*)",
                       r"\1:\2", line) # make comments executable statements that can be chained with &&

        line = re.sub(r"^\s*setup\s", "setup -j ", line)
        cmd += [line]

    del fd
    #
    # Did they ask to have group permissions honoured?
    #
    if group:
        if verbose > 2:
            print "Giving group %s r/w access" % group

        cmd += ["chgrp -R %s %s*" % (group, os.path.splitext(builder)[0])]
        cmd += ["chmod -R g+rwX %s*" % (group, os.path.splitext(builder)[0])]
        
    #
    # Write modified (== as run) build file to build_dir
    #
    bfile = os.path.join(build_dir, builder)
    if issamefile(bfile, tfile):
        print >> sys.stderr, "%s and %s are the same; not adding setups to installed build file" % \
              (bfile, tfile)
    else:
        try:
            bfd = open(bfile, "w")
            for line in cmd:
                print >> bfd, line
            del bfd
        except Exception, e:
            os.unlink(bfile)
            raise RuntimeError, ("Failed to write %s" % bfile)
        
    if verbose:
        print "Issuing commands:"
        print "\t", str.join("\n\t", cmd)

    system(str.join("\n", cmd))

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
#
# Handle distribution via pacman
#
def use_pacman_cache(package, version):
    """Return a pacman cache ID """
    
    return "pacman:%s:%s|version('%s')" % (pacman_cache, package, version)

def install_from_pacman(products_root, cacheID):
    """Install a package using pacman"""

    pacmanDir = "%s" % (products_root)
    if not os.path.isdir(pacmanDir):
        try:
            os.mkdir(pacmanDir)
        except:
            raise RuntimeError, ("Pacman failed to create %s" % (pacmanDir))

    if verbose > 0:
        print >> sys.stderr, "installing pacman cache %s into %s" % (cacheID, pacmanDir)
    
    try:
        system("""cd %s && pacman -install "%s" """ % (pacmanDir, cacheID))
    except:
        raise RuntimeError, ("Pacman failed to install %s" % (cacheID))

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

def file_retrieve(file, transport):
    """Retrieve a file given a specified transport method"""

    if transport == LOCAL:
        return (file, None)
    elif transport == SCP:
        (tfile, msg) = scpretrieve(file)
    elif transport == URL:
        (tfile, msg) = urlretrieve(file)
    else:
        raise RuntimeError, "Unknown transport method: %s" % transport

    atexit.register(os.unlink, tfile)   # clean up

    return (tfile, msg)

def scpretrieve(file):
    """Retrieve a file using scp"""

    tfile = os.tmpnam()

    try:
        system("scp %s %s 2>/dev/null" % (file, tfile))
    except:
        raise RuntimeError, ("Failed to retrieve %s" % file)

    return tfile, None

def urlretrieve(file):
    """Like urllib's urlretrieve, except use urllib2 to detect 404 errors"""

    try:
        fd = urllib2.urlopen(file); del fd
    except urllib2.HTTPError:
        raise RuntimeError, ("Failed to open URL %s" % file)
    return urllib.urlretrieve(file)

HTTPError = urllib2.HTTPError

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

def listdir(Distrib, url):
    """Return a list of the files specified by a directory URL"""

    if Distrib.transport == LOCAL:
        return os.listdir(url)
    elif Distrib.transport == URL:
        """Read a URL, looking for the hrefs that apache uses in directory listings"""
        import HTMLParser, urlparse
        class LinksParser(HTMLParser.HTMLParser):
            """Based on code in Martelli's "Python in a Nutshell" """
            def __init__(self):

                HTMLParser.HTMLParser.__init__(self)
                self.nrow = 0
                self.seen = set()
                self.files = [] # files listed in table
                self.is_attribute = False # next data is value of <attribute>
                self.is_apache = False # are we reading data from apache?

            def handle_starttag(self, tag, attributes):
                if tag == "tr": # count rows in table
                    self.nrow += 1

                if tag == "address":
                    self.is_attribute = True

                if self.nrow <= 1 or tag != "a":
                    return

                for name, value in attributes:
                    if name != "href":
                        continue
                    if re.search(r"/$", value): # a directory
                        continue

                    self.files += [value]

            def handle_data(self, data):
                if self.is_attribute:
                    self.is_apache = re.search(r"^Apache", data)
                    self.is_attribute = False

        p = LinksParser()
        for line in open(url, "r").readlines():
            p.feed(line)

        if not p.is_apache:
            print >> sys.stderr, \
                  "Warning: I'm assuming that the manifest directory listing comes from an Apache server"

        return p.files
    else:
        raise AssertionError, ("I don't know how to handle transport == %s" % Distrib.transport)

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

def find_file_on_path(file, path, auxDir = None):
    """Look for a file on :-separated path, looking in auxDir if
    an element of path is empty"""
    
    for bd in path.split(":"):
        if bd == "":
            if auxDir:
                bd = auxDir
            else:
                continue
        full_file = os.path.join(bd, file)

        if os.path.exists(full_file):
            if verbose:
                print "Found %s (%s)" % (file, full_file)
            return full_file

    return None

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

def get_root_from_buildfile(buildFile):
    """Given the name of a buildfile, return (cvsroot, svnroot, url);
    presumably only one will be valid"""

    cvsroot = None; svnroot = None; url = None
    other = None                  # this build file is Other, but valid

    fd = open(buildFile)

    for line in fd:
        if re.search(r"^\s*[:#].*\bBuild\s+File\b", line, re.IGNORECASE):
            other = True

        mat = re.search(r"^\s*export\s+(CVS|SVN)ROOT\s*=\s*(\S*)", line)
        if mat:
            type = mat.group(1); val = re.sub("\"", "", mat.group(2))

            if type == "CVS":
                cvsroot = val
            elif type == "SVN":
                svnroot = val
            else:
                if verbose:
                    print >> sys.stderr, "Unknown root type:", line,

            continue

        mat = re.search(r"^\s*(cvs|svn)\s+(co|checkout)\s+(\S)", line)
        if mat:
            type = mat.group(1); val = re.sub("\"", "", mat.group(3))
            
            if type == "cvs":
                cvsroot = val
            elif type == "svn":
                svnroot = val
            else:
                if verbose:
                    print >> sys.stderr, "Unknown src manager type:", line,

            continue

        mat = re.search(r"^\s*(wget|curl)\s+(--?\S+\s+)*\s*(\S*)", line)
        if mat:
            url = mat.group(3)

    return (cvsroot, svnroot, url, other)

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

def create(Distrib, top_product, top_version, dbz, flavor, installFlavor,
           force, manifest, noaction, transport, verbose):
    """Create a distribution"""

    if not os.path.isdir(package_base):
        if verbose > 0:
            print >> sys.stderr, "Creating", package_base
        try:
            os.makedirs(package_base)
        except:
            raise RuntimeError, ("Failed to create", package_base)

    if not top_version:
        top_version = Distrib.lookup_current_version(top_product, True)

    if noeups:
        product = top_product

        ptablefile = find_file_on_path("%s.table" % product, buildFilePath)
        if not ptablefile:
            if verbose > 0:
                print >> sys.stderr, "Unable to find a table file for %s" % product
            if os.path.exists(os.path.join("ups", "%s.table" % product)):
                print >> sys.stderr, "N.b. found %s.table in ./ups; consider adding ./ups to --build path" % (product)
                
            ptablefile = "none"

        productList = [(product, top_version, None, False)]
        dependencies = eups.dependencies_from_table(ptablefile, verbose)
        if dependencies:
            for (product, version, optional) in dependencies:
                if not version:
                    version = eups.current(product)
                productList += [(product, version, installFlavor, optional)]
    else:
        productList0 = eups.dependencies(top_product, top_version, dbz)
        #
        # Now look to see if they're optional
        #
        try:
            table_dependencies = eups.dependencies_from_table(eups.table(top_product, top_version, dbz, flavor),
                                                              verbose)

            productList = []
            for (product, version, productFlavor, optionalInTable) in productList0:
                optional = optionalInTable or filter(lambda p: p[0] == product and p[2], table_dependencies) != []
                productList += [(product, version, productFlavor, optional)]
        except RuntimeError, msg:
            productList = productList0

    products = []
    for (product, version, productFlavor, optional) in productList:
        if verbose > 1:
            print "Product:", product, "  Flavor:", installFlavor, "  Version:", version

        if product == top_product and noeups:
            basedir, pdb, pdir = None, None, None
            product_dir = "/dev/null"
        else:
            try:
                (pversion, pdb, pdir, pcurrent, psetup) = eups.list(product, version, dbz, flavor)
            except KeyboardInterrupt:
                sys.exit(1)
            except:
                print >> sys.stderr, "WARNING: Failed to lookup directory for", \
                      "product:", product, "  Flavor:", installFlavor, "  Version:", version
                continue

            try:
                ptablefile = eups.table(product, version, dbz, flavor)
                if ptablefile == "":
                    ptablefile = " "
            except KeyboardInterrupt:
                sys.exit(1)                    
            except:
                print >> sys.stderr, "WARNING: Failed to lookup tablefile for", \
                      "product:", product, "  Flavor:", flavor, "  Version:", version
                continue

            if pversion != version:
                print >> sys.stderr, "Something's wrong with %s; %s != %s" % (product, version, pversion)
            #
            # We have the product's directory, and which DB it's registered in
            #
            if pdir == "none":
                basedir = ""; product_dir = pdir
            else:
                try:
                    (basedir, product_dir) = re.search(r"^(\S+)/(%s/\S*)$" % (product), pdir).groups()
                except:
                    if verbose > 1:
                        print >> sys.stderr, "Split of \"%s\" at \"%s\" failed; proceeding" \
                              % (pdir, product)
                    if False:
                        print >> sys.stderr, "WARNING: not creating package for %s" % (product)
                        continue
                    else:
                        try:
                            (basedir, product_dir) = re.search(r"^(\S+)/([^/]+/[^/]+)$", pdir).groups()
                            if verbose > 1:
                                print >> sys.stderr, "Guessing \"%s\" has productdir \"%s\"" \
                              % (pdir, product_dir)
                        except:
                            if verbose:
                                print >> sys.stderr, "Again failed to split \"%s\" into basedir and productdir" \
                                      % (pdir)

                            basedir = ""; product_dir = pdir

        if pacman:
            distID = use_pacman_cache(product, version)
        elif buildFilePath != None:
            distID = write_builder(basedir, product_dir, package_base, product, version, buildFilePath)
            if optional and not distID:
                if verbose > -1:
                    print >> sys.stderr, "Skipping optional product %s" % (product)
                continue
        else:
            distID = write_tarball(basedir, product_dir, package_base, product, version)

        if ptablefile != "none":
            fulltablename = ptablefile
            ptablefile = os.path.basename(ptablefile)
            tabledir = get_tabledir(transport)

            if product == top_product and noeups: # the file's called product.table as if it were in a product's
                     # repository, but it still needs to be installed
                tablefile_for_distrib = os.path.join(tabledir,
                                                     "%s-%s.table" % (top_product, top_version))
                ptablefile = "%s.table" % top_version
            elif ("%s.table" % (product)) == ptablefile:
                # we want the version with expanded dependencies
                tablefile_for_distrib = os.path.join(tabledir, "%s-%s.table" % (product, version))
                ptablefile = "%s.table" % version
            else:
                tablefile_for_distrib = os.path.join(tabledir, "%s-%s" % (product, ptablefile))

            if tablefile_for_distrib:
                if verbose > 1:
                    print >> sys.stderr, "Copying %s to %s" % (fulltablename, tablefile_for_distrib)
                copyfile(fulltablename, tablefile_for_distrib)

        products += [[product, installFlavor, version, pdb, pdir, ptablefile, product_dir, distID]]

        if no_dependencies:
            if force:
                break
            else:
                print >> sys.stderr, "Not writing manifest as you omitted dependencies; use --force to write it anyway"
    #
    # Time to write enough information to declare the products
    #
    manifestDir = os.path.join(package_base, "manifests")
    if not os.path.isdir(manifestDir):
        try:
            os.mkdir(manifestDir)
        except:
            raise OSError, "I failed to create %s" % (manifestDir)
    
    if not manifest:
        manifest = os.path.join(manifestDir, manifestFile(top_product, top_version))
        
    if verbose > 0:
        print >> sys.stderr, "Writing", manifest

    try:
        if not noaction:
            ofd = open(manifest, "w")
    except:
        raise OSError, ("Failed to open", manifest)

    if not noaction:
        print >> ofd, "EUPS distribution manifest for %s (%s). Version %s" % \
              (top_product, top_version, eups_distrib_version)
        
    rproducts = products[:]; rproducts.reverse() # reverse the products list
    for p in rproducts:
        (product, installFlavor, version, pdb, pdir, ptablefile, product_dir, distID) = p
        if not installFlavor:
            installFlavor = eups.flavor()
        if not noaction:
            print >> ofd, "%-15s %-12s %-10s %-25s %-25s %s" % \
                  (product, installFlavor, version, ptablefile, product_dir, distID)

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

class Current(object):
    def __init__(self, Distrib):
        self.Distrib = Distrib
        self.file = self.Distrib.find_file(currentFile())

    def read_current(current):
        """Read a list of current products from file current"""

        fd = open(current, "r")

        line = fd.readline()
        mat = re.search(r"^EUPS distribution current version list. Version (\S+)\s*$", line)
        if not mat:
            raise RuntimeError, ("First line of file %s is corrupted:\n\t%s" % (current, line),)
        version = mat.groups()[0]
        if version != eups_distrib_version:
            print >> sys.stderr, "WARNING. Saw version %s; expected %s", version, eups_distrib_version

        products = []
        for line in fd:
            line = line.split("\n")[0]
            if re.search(r"^\s*#", line):
                continue

            try:
                (product, flavor, version) = re.findall(r"\S+", line)[0:3]
            except:
                raise RuntimeError, ("Failed to parse line:", line)

            products += [(product, version)]

        return products

    def write_current(current, products, noaction = False):
        """Write a list of current products to file current"""

        if not noaction:
            ofd = open(current, "w")

        if verbose > 1:
            print >> sys.stderr, "Writing current product list to", current

        if not noaction:
            print >> ofd, "EUPS distribution current version list. Version %s" % (eups_distrib_version)

        for p in products:
            (product, version) = p[0:2]
            if not noaction:
                print >> ofd, str.join("\t", [product, installFlavor, version])

    def createCurrent(top_product, top_version, dbz, installFlavor,
                       noaction, verbose):
        """Create a list of packages that are declared current to eups distrib"""

        current = "%s/%s" % (package_base, currentFile())

        if top_product == "":               # update entire current list
            products = []
        else:
            try:
                products = read_current(current)
            except:
                products = []

            nproducts = []
            for p in products:
                if p[0] != top_product:
                    nproducts += [p]
            products = nproducts
        #
        # Extract the up-to-date information about current versions,
        # and add it to the previously existing list [if any]
        #
        if top_version:
            dp = [(top_product, top_version)]
            try:
                info = eups.list(top_product, top_version, dbz, flavor)
            except:
                print >> sys.stderr, "WARNING: failed to find a version \"%s\" of product %s" % \
                      (top_version, top_product)
        else:
            dp = [(top_product, eups.current(top_product, dbz, installFlavor))]

        products += dp

        if top_product and verbose:
            (product, version) = dp[0]
            assert (product == top_product)
            print >> sys.stderr, "Declaring version %s of %s current to eups distrib" % (version, product)
        #
        # Now write the file containing current version info
        #
        write_current(current, products, noaction)

def install(Distrib, top_product, top_version, manifest):
    """Install a set of packages"""

    manifest_product, manifest_product_version, products_root, top_version, products = \
                      Distrib.read_manifest(top_product, top_version, manifest)
    if os.path.isdir(products_root):
        if Distrib.Eups.verbose > 0:
            print >> sys.stderr, "Installing products into", products_root

    setups = []                         # setups that we've acquired while processing products
    for (productName, mflavor, versionName, tablefile, product_dir, distID) in products:
        if (Distrib.no_dependencies and 
            (productName != top_product or versionName != top_version)):
            continue
        
        info = []
        if not Distrib.noeups:
            try:
                info = Distrib.Eups.listProducts(productName, versionName)[0]
            except IndexError:
                pass

        if info and len(info) > 0 and not re.search("^LOCAL:", info[1]):
            if productName != top_product:
                setups += ["setup %s %s &&" % (productName, versionName)]
            
            if Distrib.current and not Distrib.Eups.force:
                Distrib.Eups.declareCurrent(productName, versionName, info[3])
                continue
            else:
                print >> sys.stderr, "Product %s (version %s, flavor %s) is already declared" % \
                      (productName, versionName, Distrib.Eups.flavor)
                if Distrib.Eups.force:
                    print >> sys.stderr, "Reinstalling %s anyway" % (productName)
                    eups.undeclare(productName, versionName, flavor, dbz, current, noaction)
                else:
                    continue
        #
        # We need to install and declare this product
        #
        dodeclare = True

        if distID == "None":              # we don't know how to install this product
            if verbose > 0:
                print >> sys.stderr, "I don't know how to install %s" % (productName)
            setups += ["setup %s %s &&" % (productName, versionName)]
            dodeclare = False
        else:
            method = None
            mat = re.search(r"(build|pacman):(.*)", distID)
            if mat:
                method = mat.group(1)
                cacheID = mat.group(2)

            if method == "pacman":
                install_from_pacman(products_root, cacheID)
            elif method == "build":
                install_from_builder(setups, products_root, package_base, cacheID, tmp_dir)
                setups += ["setup %s %s &&" % (productName, versionName)]
            else:
                install_from_tarball(products_root, package_base, distID)
        #
        # We may be done
        #
        if noeups:
            return
        #
        # we need to see if someone (e.g. the build/pacman script) declared the package for us
        #
        declared = eups.list(productName, versionName, dbz, flavor)
        if declared and len(declared) > 0 and not re.search(r"^LOCAL:", declared[0]):
            dodeclare = False
        #
        # Deal with table files if not in product root (i.e. -M files)
        #
        # If the table in the manifest file is not "<productName>.table" in the manifest file
        # the table file should be installed by eups_distrib and declared via eups declare -M
        #
        if dodeclare:
            if tablefile != "none":
                if ("%s.table" % (productName)) != tablefile:
                    tablefile = "%s-%s" % (productName, tablefile)
                    tablefile = find_file(tablefile, "%s/%s" % (productName, versionName))

            eups.declare(productName, versionName, flavor, dbz, tablefile,
                         products_root, product_dir, current, noaction)
        else:                           # we may still need to declare it current
            if current:
                eups.declareCurrent(productName, versionName, flavor, dbz, noaction)

#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

def listProducts(Distrib, top_product, top_version, current, manifest):
    """List available packages"""

    available = []                      # available products
    if current:
        fd = open(Distrib.find_file(currentFile()), "r")
        fd.readline()                   # skip header

        for line in fd.readlines():
            line = line[:-1]            # remove newline
            available += [line.split("\t")]
    else:
        #
        # That would have been easy. We need to read the list of manifests
        #
        manifestDir = Distrib.find_file("manifests")
        for file in listdir(Distrib, manifestDir):
            manifest = Distrib.find_file(file)
            manifest_product, manifest_product_version = Distrib.read_manifest(None, None, manifest)[0:2]

            available += [[manifest_product, None, manifest_product_version]]

    productList = []
    for a in available:
        manifest_product, manifest_flavor, manifest_product_version = a

        if manifest_flavor and manifest_flavor != Distrib.installFlavor:
            continue
        if top_product and top_product != manifest_product:
            continue
        if top_version and top_version != manifest_product_version:
            continue

        productList += [(manifest_product, manifest_product_version)]

    return productList
