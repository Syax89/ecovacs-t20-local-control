#!/usr/bin/env python3
# Port Python del decryptor denysvitali/ecovacs-firmware-tools (CVE-2024-52331).
import sys, os, struct, base64, hashlib, json
from Crypto.Cipher import AES

def derive(section_type, section_size):
    size_hex = format(section_size, 'x')
    s = ("ZWNvX2Z3X3RhcmdldCAECO-PT1jdSAtpx30byBtYW4" + str(section_type) +
         "y5iaW4" + size_hex + "825xxjeff-hk@126.com")
    orig_len = len(s)
    enc = base64.b64encode(s.encode()).decode()
    enc_skipped = enc[4:]
    if len(enc_skipped) > orig_len:
        enc_skipped = enc_skipped[:orig_len]
    h = hashlib.sha256(enc_skipped.encode()).hexdigest()
    iv = h[0:16].encode()
    key = h[35:51].encode()
    return key, iv

def unpad(data):
    if not data: return data
    p = data[-1]
    if p > len(data) or p > 16: return data
    if data[-p:] != bytes([p])*p: return data
    return data[:-p]

def find_sections(fw):
    out=[]; off=0; n=len(fw)
    while off < n-72:
        if off+8>n: break
        unkn1=fw[off]; typ=fw[off+1]
        unkn2=struct.unpack_from('<H',fw,off+2)[0]
        size=struct.unpack_from('<I',fw,off+4)[0]
        data_off=off+8; csum_off=data_off+size
        if size<=0 or size>n or csum_off+64>n or unkn1!=1 or typ!=1:
            off+=1; continue
        checksum=fw[csum_off:csum_off+64]
        if len(checksum)!=64:
            off+=1; continue
        out.append(dict(offset=off,unkn2=unkn2,size=size,data_off=data_off,
                        csum_off=csum_off,checksum=checksum.decode('latin1')))
        off=csum_off+64
        while off<n and fw[off]==0: off+=1
    return out

def main():
    path=sys.argv[1]; outdir=sys.argv[2]
    os.makedirs(outdir,exist_ok=True)
    fw=open(path,'rb').read()
    secs=find_sections(fw)
    print(f"Trovate {len(secs)} sezioni")
    manifest=None
    for i,s in enumerate(secs):
        stype=s['unkn2']>>12
        enc=fw[s['data_off']:s['data_off']+s['size']]
        key,iv=derive(stype,s['size'])
        # verifica checksum SHA256 su header+data
        blk=fw[s['offset']:s['offset']+8+s['size']]
        csum_ok=hashlib.sha256(blk).hexdigest()==s['checksum']
        try:
            dec=AES.new(key,AES.MODE_CBC,iv).decrypt(enc)
        except Exception as e:
            print(f"  sez {i}: errore AES {e}"); continue
        dec=unpad(dec)
        name=f"section_{i}_type{stype}_{s['size']}.bin"
        if i==0:
            try:
                manifest=json.loads(dec.decode('utf-8','ignore').rstrip('\x00').strip())
                name="manifest.json"
                open(os.path.join(outdir,name),'w').write(json.dumps(manifest,indent=2))
                print(f"  sez {i}: MANIFEST ok (csum {'OK' if csum_ok else 'NO'}) -> {name}")
                print("    ", json.dumps(manifest)[:400])
                continue
            except Exception as e:
                print(f"  sez {i}: manifest non-JSON ({e})")
        if manifest and 0 < i <= len(manifest.get('sections',[])):
            ms=manifest['sections'][i-1]
            ext={'sh_script':'.sh','bin':'.bin','fs':'.img','img':'.img'}.get(ms.get('type'),'.bin')
            name=ms.get('name','sec'+str(i))+ext
        open(os.path.join(outdir,name),'wb').write(dec)
        # identifica tipo
        magic=dec[:4]; kind='?'
        if magic==b'hsqs': kind='squashfs'
        elif magic[:2]==b'\x1f\x8b': kind='gzip'
        elif dec[:4]==b'\xd0\x0d\xfe\xed' or dec[:4]==b'\x27\x05\x19\x56': kind='uImage/FDT'
        elif dec[:2]==b'#!' or b'\n' in dec[:64] and dec[:1].isascii(): kind='text/script?'
        print(f"  sez {i}: type{stype} size={s['size']} csum={'OK' if csum_ok else 'NO'} -> {name} [{kind}] magic={magic.hex()}")
    print("fatto.")
main()
