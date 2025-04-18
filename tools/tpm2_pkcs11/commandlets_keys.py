# SPDX-License-Identifier: BSD-2-Clause
# python stdlib dependencies
import binascii
import io
import os
import struct
import sys
import yaml

from tempfile import mkstemp

from tpm2_pytss.tsskey import TSSPrivKey

# local imports
from .command import Command
from .command import commandlet
from .db import Db
from .objects import PKCS11ObjectFactory as PKCS11ObjectFactory
from .objects import PKCS11X509
from .utils import AESAuthUnwrapper
from .utils import TemporaryDirectory
from .utils import hash_pass
from .utils import rand_hex_str
from .utils import pemcert_to_attrs
from .utils import str2bool
from .utils import str2bytes
from .utils import get_pobject
from .utils import dump_blobs
from .utils import dump_tsspem
from .utils import dump_pubpem
from .utils import get_serialized_tr

from .tpm2 import Tpm2

from .pkcs11t import *  # noqa

class NewKeyCommandBase(Command):
    '''
    creates a key to a token within a tpm2-pkcs11 store.
    '''

    def generate_options(self, group_parser):
        group_parser.add_argument(
            '--label',
            help='The tokens label to import the key too.\n',
            required=True)
        group_parser.add_argument(
            '--key-label',
            help='The label of the key imported. Defaults to an integer value.\n')
        group_parser.add_argument(
            '--id',
            help='The key id. Defaults to a random 8 bytes of hex.\n',
            default=binascii.hexlify(os.urandom(8)).decode())
        group_parser.add_argument(
            '--attr-always-authenticate',
            action='store_true',
            help='Sets the CKA_ALWAYS_AUTHENTICATE attribute to CK_TRUE.\n')
        group_parser.add_argument(
            '--hierarchy-auth',
            help='The hierarchyauth, required for transient pobjects.\n',
            default='')
        pinopts = group_parser.add_mutually_exclusive_group()
        pinopts.add_argument('--sopin', help='The Administrator pin.\n'),
        pinopts.add_argument('--userpin', help='The User pin.\n'),

    # Implemented by derived class
    def new_key_create(self, pobj, objauth, hierarchyauth, tpm2, alg, privkey, passin, d):
        raise NotImplementedError('Implement: new_key')

    def new_key_init(self, label, sopin, userpin, hierarchyauth, pobj, sealobjects, tpm2, d):

        pobj_handle = get_pobject(pobj, tpm2, hierarchyauth, d)

        # Get the primary object encrypted auth value and sokey information
        # to decode it. Based on the incoming pin
        is_so = sopin != None
        pin = sopin if is_so else userpin

        pubkey = '%spub' % ('so' if is_so else 'user')
        privkey = '%spriv' % ('so' if is_so else 'user')
        saltkey = '%sauthsalt' % ('so' if is_so else 'user')

        sealpub = sealobjects[pubkey]
        sealpriv = sealobjects[privkey]
        sealsalt = sealobjects[saltkey]

        sealctx = tpm2.load(pobj_handle, pobj['objauth'], sealpriv, sealpub)

        sealauth = hash_pass(pin, salt=sealsalt)['hash']

        wrappingkey = tpm2.unseal(sealctx, sealauth)

        wrapper = AESAuthUnwrapper(wrappingkey)

        #create an auth value for the tertiary object.
        objauth = rand_hex_str()

        encobjauth = wrapper.wrap(str2bytes(objauth))

        return (encobjauth, objauth)

    def get_extra_privattrs(self, privkey):
        return {}

    @staticmethod
    def new_key_save(alg, keylabel, tid, label, privblob, pubblob,
                     tertiarypubdata, encobjauth, db, tpm2, extra_privattrs=None, extra_pubattrs=None,
                     override_keylen=None):
        token = db.gettoken(label)

        #
        # Cache the objects attributes from the public structure and other sources
        # and populate the db with the data. This allows use of the public data
        # without needed to load any objects which requires a pin to do.
        #
        y = yaml.safe_load(tertiarypubdata)

        initial_pubattrs = {}
        initial_privattrs = {}

        # add the id
        initial_privattrs.update({CKA_ID: binascii.hexlify(tid.encode()).decode()})
        initial_pubattrs.update({CKA_ID: binascii.hexlify(tid.encode()).decode()})

        # Add keylabel for ALL objects if set
        if keylabel is not None:
            initial_privattrs.update({
                CKA_LABEL: binascii.hexlify(keylabel.encode()).decode()
            })
            initial_pubattrs.update({
                CKA_LABEL: binascii.hexlify(keylabel.encode()).decode()
            })

        # add additional attrs
        if extra_privattrs:
            initial_privattrs.update(extra_privattrs)

        if initial_pubattrs and extra_pubattrs:
            initial_pubattrs.update(extra_pubattrs)

        objects = PKCS11ObjectFactory(y, tpm2, encobjauth, initial_pubattrs, initial_privattrs,
                      tpm_pub=pubblob, tpm_priv=privblob, override_keylen=override_keylen)

        # Store private to database
        db.addtertiary(token['id'], objects['private'])

        # if it's asymmetric, add a public object too
        if 'public' in objects and objects['public'] is not None:
            db.addtertiary(token['id'], objects['public'])

        return objects

    @staticmethod
    def output(objects, action):
        d = {
            'action' : action,
        }

        for k, v in objects.items():
            if v is not None:
                d[k] = { 'CKA_ID' : objects[k][CKA_ID] }

        yaml.safe_dump(d, sys.stdout, default_flow_style=False)

    def __call__(self, args):
        path = args['path']

        with Db(path) as db:

            with TemporaryDirectory() as d:
                tpm2 = Tpm2(d)

                label = args['label']
                sopin = args['sopin']
                userpin = args['userpin']
                alg = args['algorithm'] if 'algorithm' in args else None
                key_label = args['key_label']
                tid = args['id']
                hierarchyauth = args['hierarchy_auth']
                passin = args['passin'] if 'passin' in args else None

                privkey = None
                try:
                    privkey = args['privkey']
                except KeyError:
                    privkey = None

                token = db.gettoken(label)
                pobjectid = token['pid']
                pobj = db.getprimary(pobjectid)

                if userpin is None and sopin is None:
                    # Use empty PIN if the token has an empty user PIN
                    token_config = yaml.safe_load(io.StringIO(token['config']))
                    if token_config.get('empty-user-pin'):
                        userpin = ''
                    else:
                        sys.exit('error: at least one of the arguments --sopin --userpin is required')

                sealobjects = db.getsealobject(token['id'])

                encobjauth, objauth = self.new_key_init(
                    label, sopin, userpin, hierarchyauth,
                    pobj, sealobjects, tpm2, d)

                tertiarypriv, tertiarypub, tertiarypubdata = self.new_key_create(
                    pobj, objauth, hierarchyauth, tpm2, alg, privkey, passin, d)

                # handle options that can add additional attributes
                always_auth = args['attr_always_authenticate']
                priv_attrs = {CKA_ALWAYS_AUTHENTICATE : always_auth}
                priv_attrs.update(self.get_extra_privattrs(privkey))
                override_keylen = getattr(self, '_override_keylen', None)

                return NewKeyCommandBase.new_key_save(
                    alg, key_label, tid, label, tertiarypriv, tertiarypub,
                    tertiarypubdata, encobjauth, db, tpm2, extra_privattrs=priv_attrs,
                    override_keylen=override_keylen)


@commandlet("import")
class ImportCommand(NewKeyCommandBase):
    '''
    Imports a rsa key to a token within a tpm2-pkcs11 store.
    '''

    # adhere to an interface
    # pylint: disable=no-self-use
    def generate_options(self, group_parser):
        super(ImportCommand, self).generate_options(group_parser)
        group_parser.add_argument(
            '--privkey',
            help='Full path of the private key to be imported.\n',
            required=True)
        group_parser.add_argument(
            '--algorithm',
            help='The type of the key.\n',
            choices=['rsa', 'ecc',
                     'hmac', # Generic hmac, actually imported as keyedhash scheme null with proper attrs.
                     'hmac:sha1', 'hmac:sha256', 'hmac:sha384', 'hmac:sha512' ],
            required=False)
        group_parser.add_argument(
            '--passin',
            help='Password of the input private key file like OpenSSL (example: "pass:secret").\n',
            required=False)

    # Imports a new key
    def new_key_create(self, pobj, objauth, hierarchyauth, tpm2, alg, privkey, passin, d):

        pobj_handle = get_pobject(pobj, tpm2, hierarchyauth, d)

        # A plain HMAC key is just a keyedhash object with userwithauth and sign set.
        # Seal objects don't have sign (ie default tpm2_import -G keyedhash behavior.
        objattrs=None
        if alg == 'hmac':
            objattrs = 'userwithauth|sign|decrypt'
            alg = 'keyedhash'

        tertiarypriv, tertiarypub, tertiarypubdata = tpm2.importkey(
            pobj_handle, pobj['objauth'], objauth, privkey=privkey, alg=alg, passin=passin, objattrs=objattrs)

        # We have no way of knowing the keylength of an hmac key
        if alg and alg.startswith('hmac') or alg == 'keyedhash':
            self._override_keylen = os.path.getsize(privkey)

        return (tertiarypriv, tertiarypub, tertiarypubdata)

    def __call__(self, args):
        objects = super(ImportCommand, self).__call__(args)
        NewKeyCommandBase.output(objects, 'import')

@commandlet("addkey")
class AddKeyCommand(NewKeyCommandBase):
    '''
    Adds a key to a token within a tpm2-pkcs11 store.
    '''

    # adhere to an interface
    # pylint: disable=no-self-use
    def generate_options(self, group_parser):
        super(AddKeyCommand, self).generate_options(group_parser)
        group_parser.add_argument(
            '--algorithm',
            help='The type of the key.\n',
            choices=Tpm2.ALGS,
            required=True)

    # Creates a new key
    def new_key_create(self, pobj, objauth, hierarchyauth, tpm2, alg, privkey, passin, d):

        pobj_handle = get_pobject(pobj, tpm2, hierarchyauth, d)

        tertiarypriv, tertiarypub, tertiarypubdata = tpm2.create(
            pobj_handle, pobj['objauth'], objauth, alg=alg)

        return (tertiarypriv, tertiarypub, tertiarypubdata)

    def __call__(self, args):
        objects = super(AddKeyCommand, self).__call__(args)
        NewKeyCommandBase.output(objects, 'add')


@commandlet("addcert")
class AddCert(Command):
    '''
    Adds a certificate object
    '''

    # adhere to an interface
    # pylint: disable=no-self-use
    def generate_options(self, group_parser):
        group_parser.add_argument(
            '--label', help='The profile label to remove.\n', required=True)

        group_parser.add_argument(
            'cert', help='The x509 PEM certificate to add.\n')

        sub_group = group_parser.add_mutually_exclusive_group()

        sub_group.add_argument(
            '--key-label',
            help='The associated private key label.\n')

        group_parser.add_argument(
            '--key-id',
            help='The associated private key id in hex.\n')


    def __call__(self, args):

        path = args['path']
        label = args['label']
        keylabel = args['key_label']
        keyid = args['key_id']
        certpath = args['cert']

        if (keylabel is None) == (keyid is None):
            sys.exit('Expected --key-label or --key-id to be specified')

        attrs = pemcert_to_attrs(certpath)

        pkcs11_object = PKCS11X509(attrs)

        with Db(path) as db:

            # get token to add to
            token = db.gettoken(label)

            # verify that key is existing
            # XXX we should be verifying that it's expected, but I guess one could always load up a cert
            # not associated with a key.
            tobjs = db.gettertiary(token['id'])

            # look up the private key
            missing_id_or_label = None
            for t in tobjs:
                if keylabel is not None:
                    missing_id_or_label = AddCert.get_id_by_label(t, keylabel)
                else:
                    missing_id_or_label = AddCert.get_label_by_id(t, keyid)
                if missing_id_or_label is not None:
                    break

            if missing_id_or_label is None:
                raise RuntimeError('Cannot find key with id "%s"' % keylabel)

            # have valid keylabel needed id
            if keylabel:
                pkcs11_object.update({CKA_ID: missing_id_or_label})
                pkcs11_object.update({CKA_LABEL: binascii.hexlify(keylabel.encode()).decode()})
            # have valid id needed keylabel
            else:
                pkcs11_object.update({CKA_LABEL: missing_id_or_label})
                pkcs11_object.update({CKA_ID: keyid})

            # TODO verify that cert is cryptographically bound to key found

            # add the cert
            db.addtertiary(token['id'], pkcs11_object)

        NewKeyCommandBase.output({'cert' : pkcs11_object}, 'add')

    @staticmethod
    def get_id_by_label(tobj, keylabel):

        attrs = yaml.safe_load(io.StringIO(tobj['attrs']))

        if CKA_LABEL in attrs:
            x = attrs[CKA_LABEL]
            x = binascii.unhexlify(x).decode()
            if x == keylabel and attrs[CKA_CLASS] == CKO_PRIVATE_KEY:
                return attrs[CKA_ID]

        return None

    @staticmethod
    def get_label_by_id(tobj, keyid):

        attrs = yaml.safe_load(io.StringIO(tobj['attrs']))

        if CKA_ID in attrs:
            x = attrs[CKA_ID]
            if x == keyid and attrs[CKA_CLASS] == CKO_PRIVATE_KEY:
                return attrs[CKA_LABEL] if CKA_LABEL in attrs else ''

        return None

@commandlet("objmod")
class ObjMod(Command):
    '''
    Dumps and modifies objects.
    '''

    _type_map = {
        'int' : 'do_int',
        'str' : 'do_str',
        'bool': 'do_bool',
        'raw' : 'do_raw',
    }

    @staticmethod
    def do_int(value):
        return int(value, 0)

    @staticmethod
    def do_bool(value):
        return str2bool(value)

    @staticmethod
    def do_str(value):
        return binascii.hexlify(value.encode()).decode()

    @staticmethod
    def do_raw(value):
        return value

    @classmethod
    def mod(cls, path, tid, key, value, inattrs, vtype):

        with Db(path) as db:
            obj = db.getobject(tid)
            if obj is None:
                sys.exit('Not found, object with id: {}'.format(tid))
        s = obj['attrs']
        obj_attrs = yaml.safe_load(s)

        # if we don't have any update data, just dump the attributes
        if not key and not inattrs:
            print(yaml.safe_dump(obj_attrs, default_flow_style=False))
            sys.exit()

        # if we have attributes YAML file, then we want to update all attributes
        if inattrs:
            with Db(path) as db:
                y = yaml.safe_load(open(inattrs, "r"))
                db.updatetertiary(obj['id'], y)
            sys.exit()

        # else we have --key and possibly --value
        #
        # look in the CKA_ globals from pkcs11t.py file for
        # a mapping string or raw value map.
        # filter(lambda x: x.startswith('CKA_'), globals().keys())
        keys = []
        for k in globals().keys():
            if k.startswith('CKA_'):
                keys.append(k)

        keynames = {}
        for k in keys:
            keynames[globals()[k]] = k

        keyname=None
        if key in keys:
            keyname=key
            key=globals()[key]
        else:
            key = int(key, 0)
            if key not in keynames:
                sys.exit('Unknown key: %d', key)
            keyname = keynames[key]

        if not value:
            if key and not key in obj_attrs:
                sys.exit("Key not found")

            print(yaml.safe_dump({keyname : obj_attrs[key]}))
            sys.exit()

        if not vtype:
            sys.exit("When specifying a value, type is required")

        value = getattr(cls, ObjMod._type_map[vtype])(value)
        obj_attrs[key] = value
        with Db(path) as db:
            db.updatetertiary(obj['id'], obj_attrs)

    # adhere to an interface
    def generate_options(self, group_parser):
        group_parser.add_argument(
            '--id', help='The object id.\n', required=True)
        group_parser.add_argument(
            '--key',
            help='The key to dump.\n')
        group_parser.add_argument(
            '--value',
            help='The value to set.\n')
        group_parser.add_argument(
            '--type',
            choices=self._type_map.keys(),
            help='Specify the type.\n')
        group_parser.add_argument(
            'attrs', nargs='?', help='The YAML attribute file.\n')
    def __call__(self, args):

        path = args['path']

        key = args['key'] if 'key' in args else None
        value = args['value'] if 'value' in args else None
        attrs = args['attrs'] if 'attrs' in args else None

        if attrs and key:
            sys.exit('Cannot specify --key when specifying the attributes')

        if attrs and value:
            sys.exit('Cannot specify --value when specifying the attributes')

        if value and not args['type']:
            sys.exit('require --type when specifying --value')

        ObjMod.mod(path, args['id'], key, value, attrs, args['type'])


@commandlet("objdel")
class ObjDel(Command):
    '''
    Deletes an object from a token.
    '''

    @classmethod
    def delete(cls, path, tid):

        with Db(path) as db:
            obj = db.getobject(tid)
            db.rmobject(obj['id'])

    # adhere to an interface
    def generate_options(self, group_parser):
        group_parser.add_argument(
            'id', help='The id of the object to delete.\n')

    def __call__(self, args):

        path = args['path']

        ObjDel.delete(path, args['id'])

@commandlet("link")
class LinkCommand(NewKeyCommandBase):
    '''
    Imports an existing TPM key to a token within a tpm2-pkcs11 store.
    '''

    # adhere to an interface
    # pylint: disable=no-self-use
    def generate_options(self, group_parser):
        super(LinkCommand, self).generate_options(group_parser)
        group_parser.add_argument('privkey',
            nargs='*',
            help='Path of the key to be linked or persistent handle.\n')
        group_parser.add_argument(
            '--auth',
            default='',
            help='The auth value for the key to link.\n'
        )

    def new_key_init(self, label, sopin, userpin, hierarchyauth, pobj, sealobjects, tpm2, d):

        pobj_handle = get_pobject(pobj, tpm2, hierarchyauth, d)

        # Get the primary object encrypted auth value and sokey information
        # to decode it. Based on the incoming pin
        is_so = sopin != None
        pin = sopin if is_so else userpin

        pubkey = '%spub' % ('so' if is_so else 'user')
        privkey = '%spriv' % ('so' if is_so else 'user')
        saltkey = '%sauthsalt' % ('so' if is_so else 'user')

        sealpub = sealobjects[pubkey]
        sealpriv = sealobjects[privkey]
        sealsalt = sealobjects[saltkey]

        sealctx = tpm2.load(pobj_handle, pobj['objauth'], sealpriv, sealpub)

        sealauth = hash_pass(pin, salt=sealsalt)['hash']

        wrappingkey = tpm2.unseal(sealctx, sealauth)

        wrapper = AESAuthUnwrapper(wrappingkey)

        objauth = self._auth

        encobjauth = wrapper.wrap(str2bytes(objauth))

        return (encobjauth, objauth)

    def create_from_tss_key(self, pobj, objauth, hierarchyauth, tpm2, alg, keypath, d):

        keypath = keypath[0]

        with open(keypath, "rb") as f:
            keybytes = f.read()
        tss2_privkey = TSSPrivKey.from_pem(keybytes)
        is_empty_auth = tss2_privkey.empty_auth
        phandle = tss2_privkey.parent
        pubbytes = tss2_privkey.public.marshal()
        privbytes = tss2_privkey.private.marshal()

        pid = pobj['id']
        pobj_config = yaml.safe_load(pobj['config'])
        is_transient = pobj_config['transient']
        if not is_transient and (phandle == tpm2.TPM2_RH_OWNER or \
            (phandle >> tpm2.TPM2_HR_SHIFT != tpm2.TPM2_HT_PERSISTENT)):
            sys.exit('The primary object (id: {:d}) is persistent and'
                ' the TSS Engine key does not have a persistent parent,'
                ' got: 0x{:x}'.format(pid, phandle))
        elif is_transient and not (phandle == tpm2.TPM2_RH_OWNER or phandle == 0):
            # tpm2-tss-engine < 1.1.0 used a phandle of 0 instead of tpm2.TPM2_RH_OWNER
            sys.exit('The primary object (id: {:d}) is transient and'
                ' the TSS Engine key has a parent handle,'
                ' got: 0x{:x}'.format(pid, phandle))


        if is_empty_auth and len(self._auth) if self._auth is not None else 0:
            sys.exit('Key expected to have auth value, please specify via option --auth');

        if not is_transient:
            # Im diving into the ESYS_TR serialized format,
            # this isn't the smartest thing to do...
            hexhandle = pobj_config['esys-tr']
            handle_bytes = binascii.unhexlify(hexhandle)[0:4]
            expected_handle = struct.unpack(">I", handle_bytes)[0]
            if phandle != expected_handle:
                sys.exit("Key must be parent of 0x{:X}, got 0x{:X}".format(
                    expected_handle, phandle))

        pobj_handle = get_pobject(pobj, tpm2, hierarchyauth, d)
        pobjauth = pobj['objauth']
        ctx = tpm2.load(pobj_handle, pobjauth, privbytes, pubbytes)
        tertiarypubdata, _ = tpm2.readpublic(ctx, False)

        privfd, tertiarypriv = mkstemp(prefix='', suffix='.priv', dir=d)
        try:
            os.write(privfd, privbytes)
        finally:
            os.close(privfd)

        pubfd, tertiarypub = mkstemp(prefix='', suffix='.pub', dir=d)
        try:
            os.write(pubfd, pubbytes)
        finally:
            os.close(pubfd)

        return (tertiarypriv, tertiarypub, tertiarypubdata)

    def create_from_key_blobs(self, pobj, objauth, hierarchyauth, tpm2, alg, keypaths, d):

        tertiarypub = keypaths[0]
        tertiarypriv = keypaths[1]

        pobj_handle = get_pobject(pobj, tpm2, hierarchyauth, d)
        pobjauth = pobj['objauth']
        try:
            ctx = tpm2.load(pobj_handle, pobjauth, tertiarypriv, tertiarypub)
        except RuntimeError as e:
            # Try swapping pub/priv
            tmp = tertiarypub
            tertiarypub = tertiarypriv
            tertiarypriv = tmp
            ctx = tpm2.load(pobj_handle, pobjauth, tertiarypriv, tertiarypub)

        tertiarypubdata, _ = tpm2.readpublic(ctx, False)

        return (tertiarypriv, tertiarypub, tertiarypubdata)

    def create_from_persistent_handle(self, tpm2, handle, d):

        pubfd, tertiarypub = mkstemp(prefix='', suffix='.pub', dir=d)
        os.close(pubfd)
        tertiarypriv = None
        tertiarypubdata, _ = tpm2.readpublic(handle, False, pub_blob_path=tertiarypub)

        return (tertiarypriv, tertiarypub, tertiarypubdata)

    # Links a new key
    def new_key_create(self, pobj, objauth, hierarchyauth, tpm2, alg, keypaths, passin, d):

        if keypaths is None:
            sys.exit("Keypath must be specified")

        if len(keypaths) == 1:
            if LinkCommand.is_persistent_handle(keypaths[0]):
                return self.create_from_persistent_handle(tpm2, keypaths[0], d)
            else:
                return self.create_from_tss_key(pobj, objauth, hierarchyauth, tpm2, alg, keypaths, d)

        if len(keypaths) == 2:
            return self.create_from_key_blobs(pobj, objauth, hierarchyauth, tpm2, alg, keypaths, d)

        sys.exit("Expected one persistent handle or one or two keyblobs, got: {}".format(len(keypaths)))

    def get_extra_privattrs(self, keypaths):
        if len(keypaths) == 1 and LinkCommand.is_persistent_handle(keypaths[0]):
            handle = int(keypaths[0], 16)
            serialized_tr = get_serialized_tr(handle)
            extra_attrs = { CKA_TPM2_SERIALIZED_TR: serialized_tr }
            return extra_attrs
        else:
            return {}

    def is_persistent_handle(str):
        try:
            handle = int(str, 16)
        except:
            return False
        return handle >> Tpm2.TPM2_HR_SHIFT == Tpm2.TPM2_HT_PERSISTENT

    def __call__(self, args):
        self._auth = args['auth'] if 'auth' in args else None
        objects = super(LinkCommand, self).__call__(args)
        NewKeyCommandBase.output(objects, 'link')


@commandlet("export")
class Export(Command):
    '''
    Exports an object from a token to a specified format.
    '''

    @staticmethod
    def _handle_tpm_key(db, obj, pin, is_so_pin, hierarchyauth, format, output_prefix):

        attrs = yaml.safe_load(io.StringIO(obj['attrs']))
        cka_class = attrs[CKA_CLASS]      

        if cka_class == CKO_SECRET_KEY:
            if format == "pem":
                raise RuntimeError(f'A format of "{format}" is not supported for CKO_SECRET_KEY objects')

            dump_blobs(db, obj, pin, is_so_pin, output_prefix)
            return

        if cka_class == CKO_PRIVATE_KEY:
            
            if format == "tpm2":           
                dump_blobs(db, obj, pin, is_so_pin, output_prefix)
            else:
                dump_tsspem(db, obj, pin, is_so_pin, output_prefix)               
            return
            
        raise RuntimeError(f'Object CKA_CLASS not supported, got: "{cka_class}"')
    
    @staticmethod
    def _handle_public_key(db, obj, pin, is_so_pin, hierarchyauth, format, output_prefix):
        dump_pubpem(db, obj, pin, is_so_pin, output_prefix)

    @staticmethod
    def export(db, tid, pin, is_so_pin, hierarchyauth, format, output_prefix):

        if not output_prefix:
            output_prefix = str(tid)

        obj = db.getobject(tid)
   
        attrs = yaml.safe_load(io.StringIO(obj['attrs']))
        
        cka_class = attrs[CKA_CLASS]

        handler = {
            CKO_SECRET_KEY : Export._handle_tpm_key,
            CKO_PRIVATE_KEY : Export._handle_tpm_key,
            CKO_PUBLIC_KEY : Export._handle_public_key,
        }
        
        if cka_class not in handler:
            raise RuntimeError(f'Object class not supported, got: "{cka_class}"')
        
       
        handler[cka_class](db, obj, pin, is_so_pin, hierarchyauth, format, output_prefix)
            
    def generate_options(self, group_parser):
        group_parser.add_argument(
            '--id', help='The id of the object to add (mutually exclusive with --label and --key-label).\n',
            type=int)

        group_parser.add_argument(
            '--label', help='The token label of the object to export. Requires --key-label\n')

        group_parser.add_argument(
            '--key-label', help='The label of the object to export. Required --label\n')

        group_parser.add_argument(
            '--format',
            choices = [ 'auto', 'tpm2', 'pem' ],
            default = 'auto',
            help='The format of the object to dump.\n')

        group_parser.add_argument(
            '--hierarchy-auth',
            help='The authorization password for the owner hierarchy when using a token with a transient primary object\n',
            default="")

        group_parser.add_argument(
            '--output-prefix',
            help='The output prefix. ' +
                'tpm2 blobs are output as suffix.priv and suffix.blob, pem outputs are suffix.pem\n')

        pinopts = group_parser.add_mutually_exclusive_group()
        pinopts.add_argument('--sopin', help='The Administrator pin.\n'),
        pinopts.add_argument('--userpin', help='The User pin.\n'),

    def __call__(self, args):

        object_id = args["id"]
        object_label = args["key_label"]
        token_label = args["label"]
        output_prefix = args['output_prefix']
        
        if object_id and object_label:
            sys.exit("Cannot specify --id with --key-label")

        elif object_id and token_label:
            sys.exit("Cannot specify --id with --label")

        elif not object_id and not token_label and not object_label:
            sys.exit("Must specify --label and --key-label or --id")

        path = args['path']

        is_so_pin = False

        pin = None        
        sopin = args['sopin']
        userpin = args['userpin']
        
        if sopin:
            pin = args['sopin']
            is_so_pin = True
        elif userpin:
            pin = args['userpin']

        with Db(path) as db:
            
            if not object_id:
                token = db.gettoken(label=token_label)
                objects = db.getobjects(token['id'])
                for obj in objects:
                    if AddCert.get_id_by_label(obj, object_label):
                        object_id = obj['id']
                        break
            
                if not object_id:
                    sys.exit(f'Could not find object with label "{object_label}" in token "{token_label}"')

                if not output_prefix:
                    output_prefix = object_label

            Export.export(db, object_id, pin, is_so_pin, args['hierarchy_auth'], args['format'], output_prefix)
