const fse = require('fs-extra')
const md5 = require('md5-file')
const glob = require('glob')
const CSV = require('papaparse')
const path = require('path')
const pako = require('pako')

const Glob = glob.Glob
glob.promise = function (pattern, options) {
  return new Promise(function (resolve, reject) {
    var g = new Glob(pattern, options)
    g.once('end', resolve)
    g.once('error', reject)
  })
}

const readCsv = async (csvPath, silence) => {
  try {
    const data = await new Promise((rev, rej) => {
      fse.readFile(csvPath, 'utf-8', (err, data) => {
        if (err) rej(err)
        rev(data)
      })
    })
    return CSV.parse(data.replace(/^\ufeff/, ''), { header: true }).data
  } catch (err) {
    if (!silence) {
      console.error(`读取csv失败：${err.message}\n${err.stack}`)
    }
    return []
  }
}

const writeCsv = async (csvPath, list) => {
  try {
    const text = CSV.unparse(list)
    await new Promise((rev, rej) => {
      fse.writeFile(csvPath, text, (err) => {
        if (err) rej(err)
        rev()
      })
    })
  } catch (err) {
    console.error(`写入csv失败：${err.message}\n${err.stack}`)
  }
}

const collectStoryId = async () => {
  console.log('story...')
  const files = await glob.promise('./story/**/*.csv')
  const chapterName = []
  const titleSet = new Set()
  const result = []
  await fse.ensureDir('./dist/blhxfy/story/')
  const prims = files.map(async file => {
    const list = await readCsv(file)
    const shortList = []
    let translatorName = ''
    let csvHash = ''
    shortList.push({
      id: 'filename',
      trans: path.basename(file, '.csv')
    })
    for (let i = list.length - 1; i >= 0; i--) {
      let infoLoaded = false
      if (list[i].id && list[i].trans && list[i].id !== '译者') {
        shortList.push({
          id: list[i].id,
          trans: list[i].trans
        })
      }
      if (!infoLoaded && list[i].id === 'info') {
        if (list[i].trans) {
          const name = list[i].trans.trim()
          if (name) {
            try {
              csvHash = (await md5(file)).slice(0, 7)
              result.push([name, file.replace(/^\.\/story\//, ''), `${csvHash}.csv`])
              infoLoaded = true
            } catch (e) {
              console.log(e.message)
            }
          }
        }
      } else if (/\d-chapter_name/.test(list[i].id)) {
        if (list[i].trans) {
          const trans = list[i].trans.trim()
          let title = list[i].text || list[i].jp
          title = title.trim()
          if (!titleSet.has(title) && title && trans) {
            titleSet.add(title)
            chapterName.push([title, trans])
          }
        }
      } else if (list[i].id === '译者') {
        let arr = []
        for (let key in list[i]) {
          if (key !== 'id' && list[i][key]) {
            arr.push(list[i][key])
          }
        }
        translatorName = arr.join('-')
        if (translatorName) {
          shortList.push({
            id: 'translator',
            trans: translatorName
          })
        }
      }
    }
    if (csvHash) {
      await writeCsv(`./dist/blhxfy/story/${csvHash}.csv`, shortList)
    }
  })
  await Promise.all(prims)
  const storyData = {}
  const storyDataPast = {}
  result.forEach(item => {
    if (item && item[0] && item[1]) {
      storyData[item[0]] = item[2]
      storyDataPast[item[0]] = item[1]
    }
  })
  let storyc = pako.deflate(JSON.stringify(storyData), { to: 'string' })
  let storyp = pako.deflate(JSON.stringify(storyDataPast), { to: 'string' })
  await fse.writeJson('./dist/story.json', storyp)
  await fse.writeJson('./dist/story-map.json', storyc)
  await fse.writeJSON('./dist/chapter-name.json', chapterName)
  await fse.writeJson('./dist/story-raw.json', storyDataPast)
}

const start = async () => {
  await fse.emptyDir('./dist/blhxfy/')
  await collectStoryId()
  await fse.outputFile('./dist/CNAME', 'blhx-ai.danmu9.com')
}

start()
